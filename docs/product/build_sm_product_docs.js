const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");
const { pathToFileURL } = require("url");
const marked = require("../product-preview/vendor/marked.umd.js");

const root = path.resolve(__dirname, "../..");
const outputPath = path.join(root, "Server_manager", "templates", "product_docs_fragment.html");
const pdfOutputPath = path.join(
  root,
  "Server_manager",
  "resources",
  "product_docs",
  "SC_Product_Maintenance_Document.pdf",
);
const documents = [
  { id: "home", file: "README.md", label: "文档导航" },
  { id: "overview", file: "01_系统总览与数据流.md", label: "系统总览与数据流" },
  { id: "components", file: "02_三端功能说明.md", label: "三端功能说明" },
  { id: "brokers", file: "03_券商接入说明.md", label: "券商接入说明" },
  { id: "operations", file: "04_维护与故障排查.md", label: "维护与故障排查" },
];

const articleIdsByFile = Object.fromEntries(documents.map((item) => [item.file, item.id]));

function plainText(html) {
  return html
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .trim();
}

function renderDocument(document) {
  const markdown = fs.readFileSync(path.join(__dirname, document.file), "utf8");
  let headingIndex = 0;
  let html = marked.parse(markdown, { gfm: true, breaks: false });
  html = html.replaceAll('class="flat-diagram"', 'class="product-docs-flat-diagram"');
  html = html.replace(/(<a\b[^>]*>)([^<]+)\.md(<\/a>)/g, "$1$2$3");
  html = html.replace(/<h([1-3])>([\s\S]*?)<\/h\1>/g, (_match, level, content) => {
    headingIndex += 1;
    const headingId = `${document.id}-section-${headingIndex}`;
    const label = plainText(content).replace(/"/g, "&quot;");
    return `<h${level} id="${headingId}" data-toc-label="${label}">${content}<a class="product-docs-heading-link" href="#${headingId}" aria-label="链接到本节">#</a></h${level}>`;
  });
  html = html.replace(/<table>/g, '<div class="product-docs-table-wrap"><table>').replace(/<\/table>/g, "</table></div>");
  for (const [file, id] of Object.entries(articleIdsByFile)) {
    for (const href of [file, encodeURI(file)]) {
      html = html.replaceAll(`href="${href}"`, `href="#product-doc-${id}"`);
    }
  }
  return `<article id="product-doc-${document.id}" class="product-docs-article" data-doc-id="${document.id}" data-doc-title="${document.label}">${html}</article>`;
}

const articles = documents.map(renderDocument).join("\n");
const primaryNav = documents.map((item, index) => `
  <a class="product-docs-nav-item${index === 0 ? " active" : ""}" href="#product-doc-${item.id}" data-doc-target="${item.id}">
    <span class="product-docs-nav-index">${String(index + 1).padStart(2, "0")}</span>
    <span>${item.label}</span>
  </a>`).join("");

const css = String.raw`
  #product-docs-root { --doc-green:#267a50; --doc-blue:#356fad; --doc-ink:#17212b; --doc-muted:#65717d; --doc-line:#dfe5e1; --doc-page:#f8faf9; color:var(--doc-ink); font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Segoe UI",sans-serif; font-size:14px; line-height:1.75; }
  #product-docs-root *, #product-docs-root *::before, #product-docs-root *::after { box-sizing:border-box; }
  #product-docs-root a { color:var(--doc-blue); }
  #product-docs-root .product-docs-toolbar { position:sticky; top:0; z-index:10; display:flex; align-items:center; gap:12px; min-height:58px; padding:8px 16px; background:rgba(255,255,255,.96); border-bottom:1px solid var(--doc-line); backdrop-filter:blur(10px); }
  #product-docs-root .product-docs-title { min-width:180px; color:var(--doc-ink); font-size:15px; font-weight:700; }
  #product-docs-root .product-docs-search-wrap { position:relative; flex:1; max-width:580px; margin-left:auto; }
  #product-docs-root .product-docs-search { width:100%; height:36px; padding:0 12px; color:var(--doc-ink); background:#f8faf9; border:1px solid #d8dfdb; border-radius:6px; outline:none; }
  #product-docs-root .product-docs-search:focus { border-color:#64a984; box-shadow:0 0 0 3px rgba(69,166,111,.12); }
  #product-docs-root .product-docs-search-results { position:absolute; top:42px; right:0; left:0; display:none; max-height:300px; overflow:auto; padding:5px; background:#fff; border:1px solid var(--doc-line); border-radius:6px; box-shadow:0 14px 30px rgba(29,44,35,.14); }
  #product-docs-root .product-docs-search-results.open { display:block; }
  #product-docs-root .product-docs-search-result { display:block; padding:8px 9px; color:var(--doc-ink); text-decoration:none; border-radius:5px; }
  #product-docs-root .product-docs-search-result:hover { background:#f0f6f2; }
  #product-docs-root .product-docs-search-result small { display:block; color:var(--doc-muted); }
  #product-docs-root .product-docs-action { display:inline-flex; height:36px; align-items:center; gap:6px; padding:0 12px; color:#36443c; background:#fff; border:1px solid var(--doc-line); border-radius:6px; cursor:pointer; text-decoration:none; white-space:nowrap; }
  #product-docs-root .product-docs-action:hover { color:#267a50; background:#f4f8f5; border-color:#b9c8bf; }
  #product-docs-root .product-docs-action:active { transform:translateY(1px); }
  #product-docs-root .product-docs-layout { display:grid; grid-template-columns:190px minmax(0,1fr) 190px; gap:22px; padding:20px 18px 44px; }
  #product-docs-root .product-docs-nav { position:sticky; top:78px; align-self:start; max-height:calc(100vh - 112px); overflow:auto; padding-right:10px; }
  #product-docs-root .product-docs-nav-label, #product-docs-root .product-docs-toc-label { margin-bottom:8px; color:#87928c; font-size:11px; font-weight:700; }
  #product-docs-root .product-docs-nav-item { display:grid; grid-template-columns:26px 1fr; gap:5px; align-items:center; min-height:38px; padding:6px 8px; color:#68736d; text-decoration:none; border:1px solid transparent; border-radius:5px; font-size:12px; }
  #product-docs-root .product-docs-nav-item:hover { color:var(--doc-green); background:#f0f6f2; }
  #product-docs-root .product-docs-nav-item.active { color:var(--doc-green); background:#eaf8f0; border-color:#c9e5d3; box-shadow:inset 3px 0 #45a66f; }
  #product-docs-root .product-docs-nav-index { color:#9aa69e; font:700 10px "Segoe UI",sans-serif; }
  #product-docs-root .product-docs-content { min-width:0; }
  #product-docs-root .product-docs-article { padding:2px 0 42px; border-bottom:1px solid var(--doc-line); }
  #product-docs-root .product-docs-article:last-child { border-bottom:0; }
  #product-docs-root .product-docs-article h1 { margin:0 0 18px; font-size:27px; line-height:1.35; letter-spacing:0; }
  #product-docs-root .product-docs-article h2 { margin:35px 0 13px; padding-top:4px; font-size:21px; line-height:1.4; letter-spacing:0; }
  #product-docs-root .product-docs-article h3 { margin:25px 0 10px; font-size:17px; line-height:1.45; letter-spacing:0; }
  #product-docs-root .product-docs-heading-link { margin-left:7px; color:#b7c0bb; text-decoration:none; font-size:.72em; opacity:0; }
  #product-docs-root .product-docs-article h1:hover .product-docs-heading-link, #product-docs-root .product-docs-article h2:hover .product-docs-heading-link, #product-docs-root .product-docs-article h3:hover .product-docs-heading-link { opacity:1; }
  #product-docs-root .product-docs-article p { margin:9px 0 14px; }
  #product-docs-root .product-docs-article ul, #product-docs-root .product-docs-article ol { padding-left:23px; }
  #product-docs-root .product-docs-article li { margin:4px 0; }
  #product-docs-root .product-docs-article li { break-inside:avoid; }
  #product-docs-root .product-docs-article blockquote { margin:14px 0 20px; padding:10px 14px; color:#526059; background:#edf6f0; border-left:4px solid #45a66f; }
  #product-docs-root .product-docs-article blockquote p { margin:0 0 5px; }
  #product-docs-root .product-docs-article blockquote p:last-child { margin-bottom:0; }
  #product-docs-root .product-docs-article code { padding:2px 5px; color:#234b37; background:#e9f0ec; border-radius:4px; font:12px Consolas,"Courier New",monospace; }
  #product-docs-root .product-docs-article pre { overflow:auto; margin:16px 0 22px; padding:15px 16px; color:#e7eee9; background:#141b17; border:1px solid #29342e; border-radius:6px; }
  #product-docs-root .product-docs-article pre code { padding:0; color:inherit; background:transparent; }
  #product-docs-root .product-docs-table-wrap { overflow:auto; margin:16px 0 23px; border:1px solid #d8dfdb; border-radius:6px; }
  #product-docs-root .product-docs-article table { width:100%; min-width:580px; border-collapse:collapse; background:#fff; }
  #product-docs-root .product-docs-article th, #product-docs-root .product-docs-article td { padding:9px 11px; text-align:left; vertical-align:top; border-bottom:1px solid #e7ebe8; }
  #product-docs-root .product-docs-article th { color:#3d4a43; background:#eef3f0; font-size:12px; }
  #product-docs-root .product-docs-article tr:last-child td { border-bottom:0; }
  #product-docs-root .product-docs-flat-diagram { margin:19px 0 24px; padding:10px; overflow:auto; background:#fff; border:1px solid #d8dfdb; border-radius:7px; }
  #product-docs-root .product-docs-flat-diagram svg { display:block; width:100%; min-width:620px; height:auto; }
  #product-docs-root .product-docs-toc { position:sticky; top:78px; align-self:start; max-height:calc(100vh - 112px); overflow:auto; padding-left:14px; border-left:1px solid var(--doc-line); }
  #product-docs-root .product-docs-toc-link { display:block; padding:4px 0; color:#6b766f; text-decoration:none; font-size:11px; line-height:1.45; }
  #product-docs-root .product-docs-toc-link.level-3 { padding-left:11px; }
  #product-docs-root .product-docs-toc-link:hover, #product-docs-root .product-docs-toc-link.active { color:var(--doc-green); }
  #product-docs-root.product-docs-dark { --doc-ink:#e5e7eb; --doc-muted:#a9b4ae; --doc-line:rgba(148,163,184,.22); background:#17211d; }
  #product-docs-root.product-docs-dark .product-docs-toolbar, #product-docs-root.product-docs-dark .product-docs-search-results, #product-docs-root.product-docs-dark .product-docs-article table, #product-docs-root.product-docs-dark .product-docs-flat-diagram { background:#1f2937; }
  #product-docs-root.product-docs-dark .product-docs-search { color:#e5e7eb; background:#0f172a; border-color:rgba(148,163,184,.24); }
  #product-docs-root.product-docs-dark .product-docs-nav-item:hover { background:rgba(69,166,111,.12); }
  #product-docs-root.product-docs-dark .product-docs-nav-item.active { background:rgba(69,166,111,.18); border-color:rgba(69,166,111,.35); }
  #product-docs-root.product-docs-dark .product-docs-article th { color:#d1d5db; background:#0f172a; }
  @media (max-width:1180px) { #product-docs-root .product-docs-layout { grid-template-columns:170px minmax(0,1fr); } #product-docs-root .product-docs-toc { display:none; } }
  @media (max-width:760px) { #product-docs-root .product-docs-toolbar { min-height:54px; padding:8px 10px; } #product-docs-root .product-docs-title { display:none; } #product-docs-root .product-docs-action span { display:none; } #product-docs-root .product-docs-action { width:36px; justify-content:center; padding:0; } #product-docs-root .product-docs-layout { display:block; padding:14px 11px 35px; } #product-docs-root .product-docs-nav { position:static; display:flex; gap:5px; max-height:none; overflow:auto; padding:0 0 12px; border-bottom:1px solid var(--doc-line); } #product-docs-root .product-docs-nav-label { display:none; } #product-docs-root .product-docs-nav-item { min-width:max-content; } #product-docs-root .product-docs-article h1 { font-size:24px; } #product-docs-root .product-docs-article h2 { font-size:19px; } #product-docs-root .product-docs-flat-diagram svg { min-width:620px; } }
  @media print { #product-docs-root .product-docs-toolbar, #product-docs-root .product-docs-nav, #product-docs-root .product-docs-toc { display:none; } #product-docs-root .product-docs-layout { display:block; padding:0; } #product-docs-root .product-docs-article { break-before:page; border:0; } #product-docs-root .product-docs-article:first-child { break-before:auto; } #product-docs-root .product-docs-article h2, #product-docs-root .product-docs-article h3 { break-after:avoid; } #product-docs-root .product-docs-flat-diagram, #product-docs-root .product-docs-table-wrap, #product-docs-root .product-docs-article pre, #product-docs-root .product-docs-article ul { break-inside:avoid; } }
`;

const fragment = `<div id="product-docs-root" class="product-docs-root" data-product-docs-version="1">
  <style>${css}</style>
  <div class="product-docs-toolbar">
    <div class="product-docs-title" data-product-docs-title>产品维护文档</div>
    <div class="product-docs-search-wrap">
      <input class="product-docs-search" data-product-docs-search type="search" placeholder="搜索标题或内容" autocomplete="off" />
      <div class="product-docs-search-results" data-product-docs-search-results></div>
    </div>
    <button class="product-docs-action" type="button" data-product-docs-print><i class="ri-printer-line" aria-hidden="true"></i><span>打印</span></button>
    <a class="product-docs-action" href="/admin/product-docs/download" data-product-docs-download><i class="ri-download-2-line" aria-hidden="true"></i><span>下载 PDF</span></a>
  </div>
  <div class="product-docs-layout">
    <nav class="product-docs-nav" aria-label="产品文档目录"><div class="product-docs-nav-label">文档目录</div>${primaryNav}</nav>
    <main class="product-docs-content" data-product-docs-content>${articles}</main>
    <aside class="product-docs-toc"><div class="product-docs-toc-label">本页目录</div><nav data-product-docs-toc></nav></aside>
  </div>
</div>`;

fs.writeFileSync(outputPath, fragment, "utf8");
console.log(`Generated ${path.relative(root, outputPath)}`);

function findEdge() {
  const candidates = [
    process.env.SC_DOCS_EDGE_PATH,
    path.join(process.env["PROGRAMFILES(X86)"] || "C:\\Program Files (x86)", "Microsoft", "Edge", "Application", "msedge.exe"),
    path.join(process.env.PROGRAMFILES || "C:\\Program Files", "Microsoft", "Edge", "Application", "msedge.exe"),
  ].filter(Boolean);
  return candidates.find((candidate) => fs.existsSync(candidate));
}

function buildPdf() {
  const edge = findEdge();
  if (!edge) {
    throw new Error("Microsoft Edge was not found; cannot build the product documentation PDF");
  }
  fs.mkdirSync(path.dirname(pdfOutputPath), { recursive: true });
  const printHtml = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>SC 证券股票交易系统产品维护文档</title><style>@page{size:A4;margin:14mm 12mm}html,body{margin:0;background:#fff}</style></head><body>${fragment}</body></html>`;
  let lastError = null;

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "sc-product-docs-"));
    const printHtmlPath = path.join(tempDir, "product_docs_print.html");
    const tempPdfPath = path.join(tempDir, "product_docs.pdf");
    fs.writeFileSync(printHtmlPath, printHtml, "utf8");
    try {
      const result = spawnSync(edge, [
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--no-first-run",
        "--no-pdf-header-footer",
        `--user-data-dir=${path.join(tempDir, "edge-profile")}`,
        `--print-to-pdf=${tempPdfPath}`,
        pathToFileURL(printHtmlPath).href,
      ], { encoding: "utf8", timeout: 120000 });
      if (fs.existsSync(tempPdfPath) && fs.statSync(tempPdfPath).size > 1024) {
        fs.copyFileSync(tempPdfPath, pdfOutputPath);
        lastError = null;
        break;
      }
      lastError = result.error || new Error((result.stderr || result.stdout || `PDF generation attempt ${attempt} failed`).trim());
    } finally {
      fs.rmSync(tempDir, { recursive: true, force: true });
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1000);
  }
  if (lastError) throw lastError;
  console.log(`Generated ${path.relative(root, pdfOutputPath)}`);
}

buildPdf();
