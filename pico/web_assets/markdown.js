/* Local markdown-it 14.1.0; raw HTML and automatic resource loading are disabled. */
(function (root) {
  'use strict';
  const parser = root.markdownit({html: false, breaks: true, linkify: true, typographer: false});
  parser.linkify.set({fuzzyLink: false, fuzzyEmail: false});
  // A bare URL followed immediately by Chinese prose must not absorb that
  // prose into its href. Explicit Markdown links retain their supplied URL.
  const matchLinks = parser.linkify.match.bind(parser.linkify);
  const matchAtStart = parser.linkify.matchAtStart.bind(parser.linkify);
  function trimLink(match) {
    if (!match || !/^https?:$/.test(match.schema)) return match;
    const boundary = match.raw.search(/[（）【】《》「」『』，。；！？]/);
    if (boundary < 0) return match;
    match.raw = match.raw.slice(0, boundary);
    match.lastIndex = match.index + boundary;
    match.text = match.url = match.raw;
    return match;
  }
  parser.linkify.matchAtStart = text => trimLink(matchAtStart(text));
  parser.linkify.match = text => {
    const matches = matchLinks(text);
    for (const match of matches || []) trimLink(match);
    return matches;
  };
  const defaultValidate = parser.validateLink;
  parser.validateLink = href => /^(https?:\/\/|mailto:)/i.test(href) && defaultValidate(href);
  parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet('target', '_blank');
    tokens[index].attrSet('rel', 'noopener noreferrer');
    return renderer.renderToken(tokens, index, options);
  };
  // Replies can refer to images, but must not load remote trackers or local
  // endpoints simply because the model emitted Markdown image syntax.
  parser.renderer.rules.image = (tokens, index) => parser.utils.escapeHtml(tokens[index].content || '图片');
  root.GoalTreeMarkdown = Object.freeze({
    render: text => parser.render(String(text ?? '')),
  });
})(globalThis);
