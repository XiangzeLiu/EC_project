(() => {
  "use strict";

  const DOCUMENTS = {
    readme: {
      title: "文档导航",
      label: "维护文档导航",
      path: "../product/README.md",
      file: "README.md",
    },
    overview: {
      title: "系统总览",
      label: "系统总览与数据流",
      path: "../product/01_系统总览与数据流.md",
      file: "01_系统总览与数据流.md",
    },
    components: {
      title: "三端功能",
      label: "三端功能说明",
      path: "../product/02_三端功能说明.md",
      file: "02_三端功能说明.md",
    },
    brokers: {
      title: "券商接入",
      label: "券商接入说明",
      path: "../product/03_券商接入说明.md",
      file: "03_券商接入说明.md",
    },
    operations: {
      title: "维护排障",
      label: "维护与故障排查",
      path: "../product/04_维护与故障排查.md",
      file: "04_维护与故障排查.md",
    },
  };

  const state = {
    currentDoc: "overview",
    cache: new Map(),
    searchIndex: [],
    headingObserver: null,
  };

  const els = {
    body: document.body,
    content: document.getElementById("documentContent"),
    currentSection: document.getElementById("currentSection"),
    nav: document.getElementById("primaryNav"),
    toc: document.getElementById("pageToc"),
    menuButton: document.getElementById("menuButton"),
    sidebarScrim: document.getElementById("sidebarScrim"),
    searchInput: document.getElementById("searchInput"),
    searchResults: document.getElementById("searchResults"),
    printButton: document.getElementById("printButton"),
    progress: document.getElementById("readingProgress"),
  };

  function slugify(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/<[^>]+>/g, "")
      .replace(/[\s/]+/g, "-")
      .replace(/[^\w\u4e00-\u9fff-]/g, "")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "") || "section";
  }

  function uniqueHeadingIds(root) {
    const used = new Map();
    root.querySelectorAll("h1, h2, h3, h4").forEach((heading) => {
      const base = slugify(heading.textContent);
      const count = used.get(base) || 0;
      used.set(base, count + 1);
      heading.id = count ? `${base}-${count + 1}` : base;
    });
  }

  function wrapTables(root) {
    root.querySelectorAll("table").forEach((table) => {
      if (table.parentElement?.classList.contains("table-wrap")) return;
      const wrapper = document.createElement("div");
      wrapper.className = "table-wrap";
      table.parentNode.insertBefore(wrapper, table);
      wrapper.appendChild(table);
    });
  }

  function styleScreenshotPlaceholders(root) {
    root.querySelectorAll("li").forEach((item) => {
      if (item.textContent.includes("截图占位")) {
        item.classList.add("shot-placeholder");
      }
    });
  }

  function buildToc(root) {
    els.toc.innerHTML = "";
    const headings = [...root.querySelectorAll("h2, h3")];
    const fragment = document.createDocumentFragment();

    headings.forEach((heading) => {
      const link = document.createElement("a");
      link.href = `#${heading.id}`;
      link.dataset.level = heading.tagName === "H2" ? "2" : "3";
      link.textContent = heading.textContent;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        heading.scrollIntoView({ behavior: "smooth", block: "start" });
        history.replaceState(null, "", `${location.pathname}?doc=${state.currentDoc}#${heading.id}`);
      });
      fragment.appendChild(link);
    });

    els.toc.appendChild(fragment);
    observeHeadings(headings);
  }

  function observeHeadings(headings) {
    if (state.headingObserver) state.headingObserver.disconnect();
    if (!("IntersectionObserver" in window)) return;

    state.headingObserver = new IntersectionObserver((entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (!visible) return;
      els.toc.querySelectorAll("a").forEach((link) => {
        link.classList.toggle("active", link.getAttribute("href") === `#${visible.target.id}`);
      });
    }, { rootMargin: "-72px 0px -72% 0px", threshold: 0 });

    headings.forEach((heading) => state.headingObserver.observe(heading));
  }

  function updateActiveNav(docId) {
    els.nav.querySelectorAll(".nav-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.doc === docId);
    });
    els.currentSection.textContent = DOCUMENTS[docId].title;
  }

  function bindMarkdownLinks(root) {
    root.querySelectorAll('a[href$=".md"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        const filename = decodeURIComponent(link.getAttribute("href").split("/").pop());
        const target = Object.entries(DOCUMENTS).find(([, doc]) => doc.file === filename);
        if (!target) return;
        event.preventDefault();
        loadDocument(target[0], { pushHistory: true });
      });
    });
  }

  async function getMarkdown(docId) {
    if (state.cache.has(docId)) return state.cache.get(docId);
    const response = await fetch(DOCUMENTS[docId].path, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const markdown = await response.text();
    state.cache.set(docId, markdown);
    return markdown;
  }

  async function loadDocument(docId, options = {}) {
    const target = DOCUMENTS[docId] ? docId : "overview";
    state.currentDoc = target;
    updateActiveNav(target);
    closeSidebar();
    closeSearch();
    els.content.innerHTML = '<div class="loading-state"><span class="loading-line"></span><span class="loading-line short"></span><span class="loading-block"></span></div>';

    try {
      const markdown = await getMarkdown(target);
      if (!window.marked) throw new Error("Markdown renderer unavailable");
      window.marked.setOptions({ gfm: true, breaks: false });
      els.content.innerHTML = window.marked.parse(markdown);
      uniqueHeadingIds(els.content);
      wrapTables(els.content);
      styleScreenshotPlaceholders(els.content);
      bindMarkdownLinks(els.content);
      buildToc(els.content);
      document.title = `${DOCUMENTS[target].label} | SC 系统维护文档`;

      if (options.pushHistory) {
        history.pushState({ doc: target }, "", `${location.pathname}?doc=${target}`);
      }

      requestAnimationFrame(() => {
        const requestedHash = options.hash || location.hash;
        if (requestedHash) {
          const anchor = document.getElementById(decodeURIComponent(requestedHash.slice(1)));
          if (anchor) anchor.scrollIntoView({ block: "start" });
        } else {
          window.scrollTo({ top: 0, behavior: options.instant ? "auto" : "smooth" });
        }
        updateReadingProgress();
      });
    } catch (error) {
      els.content.innerHTML = `<div class="load-error"><strong>文档加载失败</strong><br>请通过本地 HTTP 服务打开预览页面。<br><small>${String(error.message || error)}</small></div>`;
      els.toc.innerHTML = "";
    }
  }

  function extractSearchRecords(docId, markdown) {
    const records = [];
    const lines = markdown.split(/\r?\n/);
    let currentHeading = DOCUMENTS[docId].label;
    let currentLevel = 1;
    let body = [];

    const flush = () => {
      const text = body
        .join(" ")
        .replace(/[`*_>#|\[\]()]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      records.push({
        docId,
        docLabel: DOCUMENTS[docId].title,
        heading: currentHeading,
        level: currentLevel,
        text,
        anchor: slugify(currentHeading),
      });
      body = [];
    };

    lines.forEach((line) => {
      const match = /^(#{1,4})\s+(.+)$/.exec(line);
      if (match) {
        if (body.length || records.length === 0) flush();
        currentLevel = match[1].length;
        currentHeading = match[2].replace(/[`*_]/g, "").trim();
      } else if (line.trim() && !line.startsWith("```")) {
        body.push(line.trim());
      }
    });
    flush();
    return records.filter((record) => record.heading || record.text);
  }

  async function buildSearchIndex() {
    const entries = await Promise.all(Object.keys(DOCUMENTS).map(async (docId) => {
      try {
        return extractSearchRecords(docId, await getMarkdown(docId));
      } catch (_) {
        return [];
      }
    }));
    state.searchIndex = entries.flat();
  }

  function renderSearchResults(query) {
    const normalized = query.trim().toLowerCase();
    if (!normalized) {
      closeSearch();
      return;
    }

    const terms = normalized.split(/\s+/).filter(Boolean);
    const results = state.searchIndex
      .map((record) => {
        const heading = record.heading.toLowerCase();
        const body = record.text.toLowerCase();
        const score = terms.reduce((total, term) => {
          if (heading.includes(term)) return total + 5;
          if (body.includes(term)) return total + 1;
          return total - 20;
        }, 0);
        return { ...record, score };
      })
      .filter((record) => record.score >= terms.length)
      .sort((a, b) => b.score - a.score)
      .slice(0, 10);

    els.searchResults.innerHTML = "";
    if (!results.length) {
      els.searchResults.innerHTML = '<div class="empty-search">没有找到匹配内容</div>';
    } else {
      results.forEach((result) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "search-result";
        const excerpt = result.text.slice(0, 88) || "打开对应章节";
        button.innerHTML = `<small>${result.docLabel}</small><strong>${result.heading}</strong><span>${excerpt}</span>`;
        button.addEventListener("click", () => {
          els.searchInput.value = "";
          loadDocument(result.docId, { pushHistory: true, hash: `#${result.anchor}` });
        });
        els.searchResults.appendChild(button);
      });
    }
    els.searchResults.hidden = false;
  }

  function closeSearch() {
    els.searchResults.hidden = true;
  }

  function openSidebar() {
    els.body.classList.add("sidebar-open");
  }

  function closeSidebar() {
    els.body.classList.remove("sidebar-open");
  }

  function updateReadingProgress() {
    const scrollable = document.documentElement.scrollHeight - window.innerHeight;
    const percent = scrollable > 0 ? Math.min(100, Math.max(0, (window.scrollY / scrollable) * 100)) : 0;
    els.progress.style.width = `${percent}%`;
  }

  function initialDoc() {
    const params = new URLSearchParams(location.search);
    return DOCUMENTS[params.get("doc")] ? params.get("doc") : "overview";
  }

  els.nav.addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (!button) return;
    loadDocument(button.dataset.doc, { pushHistory: true });
  });

  els.menuButton.addEventListener("click", openSidebar);
  els.sidebarScrim.addEventListener("click", closeSidebar);
  els.searchInput.addEventListener("input", (event) => renderSearchResults(event.target.value));
  els.searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      els.searchInput.value = "";
      closeSearch();
      els.searchInput.blur();
    }
  });
  els.printButton.addEventListener("click", () => window.print());

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".search-field") && !event.target.closest(".search-results")) {
      closeSearch();
    }
  });

  window.addEventListener("scroll", updateReadingProgress, { passive: true });
  window.addEventListener("popstate", (event) => {
    loadDocument(event.state?.doc || initialDoc(), { instant: true });
  });

  buildSearchIndex();
  loadDocument(initialDoc(), { instant: true });
})();
