// CSP probe for xterm.js (step 09). Served by tools/ui-shots.sh --mock with the dashboard's exact
// security headers. Opens a terminal, writes palette and truecolor text, then reports in #result
// how many of the <style> elements xterm injected actually took effect (a style blocked by CSP has
// element.sheet === null) and whether the row font is the one xterm asked for.
//
// If <meta name="csp-nonce"> is present (xterm-nonce.html), the terminal gets a `documentOverride`
// Proxy of `document` whose createElement stamps that nonce on every <style>, which is the
// candidate mitigation for the Session manager page (step 11).
"use strict";

(function () {
  const meta = document.querySelector('meta[name="csp-nonce"]');
  const nonce = meta ? meta.getAttribute("content") : "";
  const host = document.getElementById("term");
  const result = document.getElementById("result");

  let documentOverride = null;
  if (nonce) {
    documentOverride = new Proxy(document, {
      get(target, key) {
        if (key === "createElement") {
          return (tag, opts) => {
            const el = target.createElement(tag, opts);
            if (String(tag).toLowerCase() === "style") el.setAttribute("nonce", nonce);
            return el;
          };
        }
        const v = target[key];
        return typeof v === "function" ? v.bind(target) : v;
      },
    });
  }

  const term = new Terminal({
    cursorBlink: false, fontSize: 13, scrollback: 1000, convertEol: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    theme: { background: "#fbfbf9", foreground: "#1b1b19" },
    documentOverride,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(host);
  try { fit.fit(); } catch (e) { /* no layout yet */ }
  term.write("Phoenix Harness: xterm.js CSP probe\n");
  term.write("\x1b[31mred\x1b[0m \x1b[32mgreen\x1b[0m \x1b[33myellow\x1b[0m \x1b[34mblue\x1b[0m \x1b[35mmagenta\x1b[0m \x1b[36mcyan\x1b[0m (16-colour palette)\n");
  term.write("\x1b[38;5;208m256-colour orange\x1b[0m \x1b[1mbold\x1b[0m \x1b[4munderline\x1b[0m\n");
  term.write("\x1b[38;2;200;30;160mtruecolor magenta (needs a style attribute)\x1b[0m\n");
  term.write("$ █");

  setTimeout(() => {
    const styles = Array.from(host.querySelectorAll("style"));
    const applied = styles.filter((s) => s.sheet !== null).length;
    const span = host.querySelector(".xterm-rows span");
    const font = span ? getComputedStyle(span).fontFamily : "(no rows)";
    const rows = host.querySelectorAll(".xterm-rows > div").length;
    const attrs = host.querySelectorAll(".xterm-rows [style]").length;
    const attrsApplied = Array.from(host.querySelectorAll(".xterm-rows [style]")).filter((e) => e.style.length > 0).length;
    result.textContent =
      `nonce=${nonce ? "yes" : "no"}; rows=${rows}; style elements applied ${applied}/${styles.length}; ` +
      `row font="${font}"; truecolor style attributes applied ${attrsApplied}/${attrs}`;
  }, 400);
})();
