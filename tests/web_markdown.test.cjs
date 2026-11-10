const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../pico/web_assets');
const sandbox = {};
vm.createContext(sandbox);
for (const name of ['markdown-it.min.js', 'markdown.js']) {
  vm.runInContext(fs.readFileSync(path.join(assets, name), 'utf8'), sandbox);
}
const render = text => sandbox.GoalTreeMarkdown.render(text);

test('headings, nested lists and emphasis have semantic hierarchy', () => {
  const html = render('# 总目标\n\n## 第一阶段\n\n### 子阶段\n\n**重要**和*备注*\n\n1. 大项\n   - 小项\n   - 第二项');
  for (const tag of ['h1', 'h2', 'h3', 'strong', 'em', 'ol', 'ul']) assert.match(html, new RegExp('<' + tag + '>'));
  assert.match(html, /<ol>[\s\S]*<li>大项[\s\S]*<ul>/);
  assert.doesNotMatch(html, /## 第一阶段|\*\*重要\*\*/);
});

test('tables, quotes, code and line breaks render without changing code', () => {
  const html = render('> 引用\n\n| 项目 | 状态 |\n| --- | --- |\n| 规划 | 完成 |\n\n正文\n换行与 `x < 2`\n\n```python\nprint("<script>")\n```');
  assert.match(html, /<blockquote>/);
  assert.match(html, /<table>/);
  assert.match(html, /<th>项目<\/th>/);
  assert.match(html, /<br>/);
  assert.match(html, /<code>x &lt; 2<\/code>/);
  assert.match(html, /<pre><code class="language-python">print\(&quot;&lt;script&gt;&quot;\)/);
});

test('raw HTML, SVG, handlers and model scripts remain inert text', () => {
  const html = render('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n<svg onload=alert(1)>');
  assert.doesNotMatch(html, /<(?:script|img|svg)\b/i);
  assert.match(html, /&lt;script&gt;/);
});

test('executable, local and relative links are not clickable', () => {
  for (const url of ['javascript:alert%281%29', 'jav&#x61;script:alert%281%29',
                     'data:text/html,test', 'vbscript:msgbox%281%29', 'file:///C:/secret', '/api/tasks', '//host/path']) {
    assert.doesNotMatch(render(`[打开](${url})`), /<a\b/, url);
  }
  const html = render('[文档](https://example.com/docs) [邮件](mailto:help@example.com)');
  assert.match(html, /href="https:\/\/example.com\/docs"/);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /href="mailto:help@example.com"/);
});

test('images never load remote trackers, data URLs or local endpoints', () => {
  for (const url of ['https://example.com/tracker.png', 'data:image/png;base64,AAAA', '/api/tasks']) {
    const html = render(`![说明](${url})`);
    assert.doesNotMatch(html, /<img\b|src=/);
    assert.match(html, /说明/);
  }
});

test('plain public source URLs are clickable without enabling scripts or loading images', () => {
  const html=render('来源：https://docs.python.org/3/\nhttps://example.org/search?q=goals&limit=3\njavascript:alert(1)');
  assert.match(html,/href="https:\/\/docs.python.org\/3\/"/);
  assert.match(html,/href="https:\/\/example.org\/search\?q=goals&amp;limit=3"/);
  assert.doesNotMatch(html,/href="javascript:|<script|<img/i);
  assert.doesNotMatch(render('docs.python.org/zh-cn/3/（Python 文档）'),/<a\b/);
});

test('incomplete streaming Markdown renders safely and is complete on the next render', () => {
  assert.doesNotThrow(() => render('## 阶段\n\n**尚未结束\n\n```python\nprint(1)'));
  assert.match(render('**完整**'), /<strong>完整<\/strong>/);
});

test('Chinese prose after bare source URLs stays outside the clickable target', () => {
  const html=render('https://sci.stu.edu.cn/info/1107/11615.htm（快证据：首页快照第 56 行）');
  assert.match(html,/href="https:\/\/sci.stu.edu.cn\/info\/1107\/11615.htm"/);
  assert.match(html,/<\/a>（快证据：首页快照第/);
  assert.doesNotMatch(html,/href="[^"]*%EF%BC%88/);
  assert.match(render('https://example.org/中文资料.htm。已核对'),/href="https:\/\/example.org\/%E4%B8%AD%E6%96%87%E8%B5%84%E6%96%99.htm"/);
  assert.match(render('[明确地址](https://example.org/a（1）.htm)'),/href="https:\/\/example.org\/a%EF%BC%881%EF%BC%89.htm"/);
});

test('actual chat renderer formats only assistant replies; user and error text stays literal', () => {
  class Element {
    constructor(tag) { this.tagName=tag; this.children=[]; this.classList={add() {}}; }
    append(...children) { this.children.push(...children); }
    cloneNode() { return new Element(this.tagName); }
    replaceChildren(fragment) { this.children=fragment.children; }
  }
  const elements=new Map();
  sandbox.document={
    getElementById(id) { if(!elements.has(id))elements.set(id,new Element('div')); return elements.get(id); },
    createElement: tag=>new Element(tag), createDocumentFragment: ()=>new Element('fragment'),
  };
  let source=fs.readFileSync(path.join(assets,'app.js'),'utf8');
  vm.runInContext(source.slice(0,source.lastIndexOf('init();'))+'\nglobalThis.renderMessages=showMessages;',sandbox);
  sandbox.renderMessages([
    {role:'user',content:'**literal** <img src=x>'},
    {role:'assistant',content:'## 规划\n\n**重点**'},
    {role:'assistant',content:'<script>error evidence</script>',error:true},
  ]);
  const bodies=elements.get('conversation').children.map(e=>e.children[1]);
  assert.equal(bodies[0].textContent,'**literal** <img src=x>');
  assert.equal(bodies[0].innerHTML,undefined);
  assert.match(bodies[1].innerHTML,/<h2>规划<\/h2>/);
  assert.match(bodies[1].innerHTML,/<strong>重点<\/strong>/);
  assert.equal(bodies[2].textContent,'<script>error evidence</script>');
  assert.equal(bodies[2].innerHTML,undefined);
});
