(function () {
  function ensureLightbox() {
    let lightbox = document.querySelector(".yikes-mermaid-lightbox");
    if (lightbox) return lightbox;

    lightbox = document.createElement("div");
    lightbox.className = "yikes-mermaid-lightbox";
    lightbox.setAttribute("aria-hidden", "true");
    lightbox.innerHTML = [
      '<div class="yikes-mermaid-lightbox__toolbar">',
      '<button class="yikes-mermaid-lightbox__button" data-yikes-zoom="out" type="button" title="Zoom out">-</button>',
      '<button class="yikes-mermaid-lightbox__button" data-yikes-zoom="reset" type="button" title="Reset zoom">100%</button>',
      '<button class="yikes-mermaid-lightbox__button" data-yikes-zoom="in" type="button" title="Zoom in">+</button>',
      '<button class="yikes-mermaid-lightbox__button" data-yikes-close type="button" title="Close">Esc</button>',
      "</div>",
      '<div class="yikes-mermaid-lightbox__canvas">',
      '<div class="yikes-mermaid-lightbox__content"></div>',
      "</div>",
    ].join("");
    document.body.appendChild(lightbox);
    return lightbox;
  }

  function setScale(lightbox, scale) {
    lightbox.dataset.scale = String(scale);
    const content = lightbox.querySelector(".yikes-mermaid-lightbox__content");
    if (content) content.style.transform = "scale(" + scale + ")";
  }

  function closeLightbox() {
    const lightbox = document.querySelector(".yikes-mermaid-lightbox");
    if (!lightbox) return;
    lightbox.setAttribute("aria-hidden", "true");
    const content = lightbox.querySelector(".yikes-mermaid-lightbox__content");
    if (content) content.replaceChildren();
    document.documentElement.style.overflow = "";
  }

  function openLightbox(block) {
    const svg = block.matches("svg") ? block : block.querySelector("svg");
    if (!svg) return;
    const lightbox = ensureLightbox();
    const content = lightbox.querySelector(".yikes-mermaid-lightbox__content");
    if (!content) return;

    const clone = svg.cloneNode(true);
    clone.removeAttribute("style");
    content.replaceChildren(clone);
    setScale(lightbox, 1);
    lightbox.setAttribute("aria-hidden", "false");
    document.documentElement.style.overflow = "hidden";
  }

  function attachMermaidHandlers(root) {
    root.querySelectorAll(".mermaid").forEach(function (block) {
      if (block.dataset.yikesLightbox === "1") return;
      block.dataset.yikesLightbox = "1";
      block.setAttribute("role", "button");
      block.setAttribute("tabindex", "0");
      block.setAttribute("aria-label", "Open Mermaid diagram in larger view");
      block.addEventListener("click", function () {
        openLightbox(block);
      });
      block.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openLightbox(block);
        }
      });
    });
  }

  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const close = target.closest("[data-yikes-close]");
    if (close) {
      closeLightbox();
      return;
    }

    const zoom = target.closest("[data-yikes-zoom]");
    if (!zoom) return;

    const lightbox = ensureLightbox();
    const current = Number(lightbox.dataset.scale || "1");
    const action = zoom.getAttribute("data-yikes-zoom");
    if (action === "in") setScale(lightbox, Math.min(3, current + 0.25));
    if (action === "out") setScale(lightbox, Math.max(0.5, current - 0.25));
    if (action === "reset") setScale(lightbox, 1);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeLightbox();
  });

  let observerStarted = false;

  function startObserver() {
    if (observerStarted || !document.body) return;
    observerStarted = true;
    const observer = new MutationObserver(function () {
      attachMermaidHandlers(document);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  function initLightbox() {
    attachMermaidHandlers(document);
    startObserver();
  }

  if (
    typeof document$ !== "undefined" &&
    document$ &&
    typeof document$.subscribe === "function"
  ) {
    document$.subscribe(initLightbox);
  } else {
    document.addEventListener("DOMContentLoaded", initLightbox);
  }
})();
