# Markdown renderer

Bundled dependency: markdown-it 14.1.0, MIT license.

- Upstream: https://github.com/markdown-it/markdown-it
- Package: https://registry.npmjs.org/markdown-it/14.1.0
- `markdown-it.min.js`: unmodified `package/dist/markdown-it.min.js` from the npm tarball.
- `markdown-it.LICENSE.txt`: upstream license, retained in package distributions.
- Bundle SHA256: `38c70a1e7ca91ab40e2d9e6e60129851a717ed1c7d4acbbdd41bf9503791cf68`.

The npm tarball's SHA512 integrity was checked against registry metadata before extraction. No npm scripts were executed. Assets are served locally; the browser does not require a CDN.

`markdown.js` uses `html: false`, allows only explicit HTTP/HTTPS/mailto links, adds `noopener noreferrer`, and renders image descriptions without loading image resources. User messages and error evidence remain literal text. `innerHTML` is used only for this configured renderer's output, never directly for model text.
