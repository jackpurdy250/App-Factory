/* App Factory - three panes over one socket.
 *
 * The client is deliberately thin. It never decides anything: it renders the
 * frames the server sends and posts raw command lines back. In particular it
 * does NOT validate commands - the Python parser is the only authority on
 * what is valid, so a line typed here reaches it untouched and the operator
 * sees exactly the documented response.
 *
 * Two frame types arrive on the socket:
 *   {"type": "event",  "payload": Event.to_dict()}
 *   {"type": "status", "payload": pipeline status payload}
 */

(function () {
  "use strict";

  var MAX_LOG_NODES = 2000;
  var MAX_CLI_LINES = 400;

  var els = {
    conn: document.getElementById("conn"),
    project: document.getElementById("st-project"),
    status: document.getElementById("st-status"),
    stage: document.getElementById("st-stage"),
    iter: document.getElementById("st-iter"),
    blockers: document.getElementById("st-blockers"),
    tokens: document.getElementById("st-tokens"),
    run: document.getElementById("st-run"),
    previewEmpty: document.getElementById("preview-empty"),
    previewFrame: document.getElementById("preview-frame"),
    previewSplit: document.getElementById("preview-split"),
    previewFiles: document.getElementById("preview-files"),
    previewSource: document.getElementById("preview-source"),
    previewMeta: document.getElementById("preview-meta"),
    previewChannels: document.getElementById("preview-channels"),
    log: document.getElementById("log"),
    showPayloads: document.getElementById("show-payloads"),
    cliOut: document.getElementById("cli-out"),
    cliForm: document.getElementById("cli-form"),
    cliInput: document.getElementById("cli-input")
  };

  var socket = null;
  var attempt = 0;
  var history = [];
  var cursor = 0;

  // -- connection -------------------------------------------------------

  function connect() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(proto + "//" + window.location.host + "/ws");

    socket.addEventListener("open", function () {
      attempt = 0;
      setConnected(true);
      // The server replays history on every connect, so clear both scrollers
      // first or a reconnect would duplicate the whole run.
      els.log.textContent = "";
      els.cliOut.textContent = "";
    });

    socket.addEventListener("close", function () {
      setConnected(false);
      scheduleReconnect();
    });

    socket.addEventListener("error", function () {
      try {
        socket.close();
      } catch (err) {
        /* the close handler schedules the retry */
      }
    });

    socket.addEventListener("message", function (message) {
      var frame;
      try {
        frame = JSON.parse(message.data);
      } catch (err) {
        return;
      }
      if (!frame || typeof frame !== "object") {
        return;
      }
      if (frame.type === "status") {
        applyStatus(frame.payload || {});
      } else if (frame.type === "event") {
        applyEvent(frame.payload || {});
      }
    });
  }

  function scheduleReconnect() {
    attempt = Math.min(attempt + 1, 6);
    window.setTimeout(connect, 250 * Math.pow(2, attempt - 1));
  }

  function setConnected(live) {
    els.conn.textContent = live ? "live" : "offline";
    els.conn.className = live ? "up" : "down";
  }

  // -- header -----------------------------------------------------------

  function applyStatus(payload) {
    setText(els.project, payload.slug);
    setText(els.status, payload.status);
    els.status.className =
      "s-" + String(payload.status || "idle").replace(/_/g, "-");
    setText(els.stage, payload.stage);
    setText(els.iter, payload.iteration);
    setText(els.blockers, payload.open_blockers);
    els.blockers.className = Number(payload.open_blockers) > 0 ? "bad" : "";
    els.tokens.textContent =
      compact(payload.tokens_used) + " / " + compact(payload.max_tokens);
    setText(els.run, payload.run_id);
    document.body.classList.toggle("busy", Boolean(payload.busy));
  }

  function setText(node, value) {
    if (value === null || value === undefined || value === "") {
      node.textContent = "\u2013";
    } else {
      node.textContent = String(value);
    }
  }

  function compact(value) {
    var number = Number(value || 0);
    if (number >= 1000) {
      return String(Math.round(number / 100) / 10) + "k";
    }
    return String(number);
  }

  // -- events -----------------------------------------------------------

  function applyEvent(event) {
    switch (event.channel) {
      case "cli":
        writeCli(event.text, "reply");
        break;
      case "preview":
        showPreview(event.detail || {});
        break;
      case "status":
        applyStatus(event.detail || {});
        break;
      default:
        writeLog(event);
    }
  }

  function writeCli(line, kind) {
    var row = document.createElement("div");
    row.className = "cli-line " + (kind || "reply");
    row.textContent = line;
    els.cliOut.appendChild(row);
    while (els.cliOut.childNodes.length > MAX_CLI_LINES) {
      els.cliOut.removeChild(els.cliOut.firstChild);
    }
    els.cliOut.scrollTop = els.cliOut.scrollHeight;
  }

  function writeLog(event) {
    var scroller = els.log.parentNode;
    var pinned = nearBottom(scroller);

    var row = document.createElement("div");
    row.className = "ev " + severity(event);
    row.textContent = event.rendered || event.text || "";
    els.log.appendChild(row);

    if (event.detail) {
      var payload = document.createElement("pre");
      payload.className = "payload";
      payload.textContent = JSON.stringify(event.detail, null, 2);
      payload.hidden = !els.showPayloads.checked;
      els.log.appendChild(payload);
    }

    while (els.log.childNodes.length > MAX_LOG_NODES) {
      els.log.removeChild(els.log.firstChild);
    }
    if (pinned) {
      scroller.scrollTop = scroller.scrollHeight;
    }
  }

  function severity(event) {
    if (event.continuation) {
      return "cont";
    }
    var code = String(event.code || "");
    if (code === "..") {
      return "trace";
    }
    if (code === "HALT") {
      return "bad";
    }
    if (code === "SHIP") {
      return "good";
    }
    if (code === "G5" || code === "G7" || code === "G8") {
      return "gov";
    }
    if (event.status === "error" || event.status === "refused") {
      return "bad";
    }
    return "";
  }

  function nearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }

  // -- Window 2 ---------------------------------------------------------

  // Window 2 carries two channels: the live preview, and the frozen build
  // that `#ship` sealed. Each keeps its own detail, so switching back to
  // the preview after shipping costs nothing and loses nothing.
  var channels = { preview: null, shipped: null };
  var activeSurface = "preview";

  function showPreview(detail) {
    var url = detail.url || "";
    if (!url) {
      return;
    }
    var surface = detail.surface === "shipped" ? "shipped" : "preview";
    channels[surface] = detail;
    activeSurface = surface;
    els.previewEmpty.hidden = true;
    els.previewChannels.hidden = false;
    syncChannels();
    renderChannel(surface);
  }

  function syncChannels() {
    var buttons = els.previewChannels.querySelectorAll("button.chan");
    for (var i = 0; i < buttons.length; i += 1) {
      var surface = buttons[i].getAttribute("data-surface");
      buttons[i].disabled = !channels[surface];
      buttons[i].className = surface === activeSurface ? "chan is-on" : "chan";
    }
  }

  function renderChannel(surface) {
    var detail = channels[surface];
    if (!detail) {
      return;
    }
    var url = detail.url || "";
    els.previewMeta.textContent =
      (detail.build_id ? detail.build_id + "  " : "") +
      (surface === "shipped" ? "sealed  " : "") +
      url;

    // Non-web stacks have nothing to execute, so their build is shown as a
    // file tree plus source rather than run in a frame.
    if (detail.channel === "source") {
      els.previewFrame.hidden = true;
      els.previewSplit.hidden = false;
      renderManifest(detail);
      loadSource(url);
      return;
    }

    els.previewSplit.hidden = true;
    els.previewFrame.hidden = false;
    // Cache-bust so a new iteration at the same path cannot show stale output.
    els.previewFrame.src =
      url + (url.indexOf("?") === -1 ? "?" : "&") + "t=" + Date.now();
  }

  // Every artifact is served from the same build directory as the
  // entrypoint, so the directory URL is the entrypoint URL minus its path.
  function baseUrl(detail) {
    var url = detail.url || "";
    var entry = detail.entrypoint || "";
    if (entry && url.length > entry.length) {
      if (url.slice(url.length - entry.length) === entry) {
        return url.slice(0, url.length - entry.length);
      }
    }
    return url.slice(0, url.lastIndexOf("/") + 1);
  }

  function humanSize(bytes) {
    if (typeof bytes !== "number") {
      return "";
    }
    if (bytes < 1024) {
      return bytes + " B";
    }
    return Math.round(bytes / 102.4) / 10 + " kB";
  }

  function renderManifest(detail) {
    var files = detail.manifest || [];
    var base = baseUrl(detail);
    els.previewFiles.textContent = "";
    for (var i = 0; i < files.length; i += 1) {
      els.previewFiles.appendChild(fileRow(files[i], base, detail.entrypoint));
    }
  }

  function fileRow(file, base, entrypoint) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "file-row";
    if (file.path === entrypoint) {
      row.className = "file-row is-entry";
    }

    var name = document.createElement("span");
    name.className = "file-path";
    name.textContent = file.path;
    row.appendChild(name);

    // Size and digest come from the sealed manifest, so the tree reports
    // what was hashed rather than whatever the browser happens to fetch.
    var meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent =
      humanSize(file.size_bytes) +
      (file.sha256 ? "  " + String(file.sha256).slice(0, 12) : "");
    row.appendChild(meta);

    row.addEventListener("click", function () {
      var rows = els.previewFiles.querySelectorAll("button.file-row");
      for (var i = 0; i < rows.length; i += 1) {
        rows[i].classList.remove("is-open");
      }
      row.classList.add("is-open");
      loadSource(base + file.path);
    });
    return row;
  }

  function loadSource(url) {
    window
      .fetch(url, { cache: "no-store" })
      .then(function (response) {
        return response.ok ? response.text() : "cannot read " + url;
      })
      .then(function (body) {
        els.previewSource.textContent = body;
      })
      .catch(function (err) {
        els.previewSource.textContent = String(err);
      });
  }

  // -- input ------------------------------------------------------------

  els.previewChannels.addEventListener("click", function (event) {
    var node = event.target;
    while (node && node !== els.previewChannels && node.className !== undefined) {
      if (String(node.className).indexOf("chan") === 0) {
        break;
      }
      node = node.parentNode;
    }
    if (!node || node === els.previewChannels || node.disabled) {
      return;
    }
    activeSurface = node.getAttribute("data-surface");
    syncChannels();
    renderChannel(activeSurface);
  });

  els.showPayloads.addEventListener("change", function () {
    var nodes = els.log.querySelectorAll("pre.payload");
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].hidden = !els.showPayloads.checked;
    }
  });

  els.cliForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var line = els.cliInput.value;
    if (!line) {
      return;
    }
    els.cliInput.value = "";
    history.push(line);
    cursor = history.length;
    writeCli("> " + line, "echo");

    if (!socket || socket.readyState !== WebSocket.OPEN) {
      writeCli("offline", "bad");
      return;
    }
    socket.send(JSON.stringify({ line: line }));
  });

  els.cliInput.addEventListener("keydown", function (event) {
    if (event.key === "ArrowUp") {
      if (cursor > 0) {
        cursor -= 1;
        els.cliInput.value = history[cursor];
        event.preventDefault();
      }
    } else if (event.key === "ArrowDown") {
      if (cursor < history.length - 1) {
        cursor += 1;
        els.cliInput.value = history[cursor];
      } else {
        cursor = history.length;
        els.cliInput.value = "";
      }
      event.preventDefault();
    }
  });

  document.addEventListener("click", function (event) {
    // Clicking anywhere outside the log or the preview returns the caret to
    // the prompt, which is where a terminal operator expects it to be.
    var target = event.target;
    if (target === document.body || (target.closest && target.closest("#w1"))) {
      els.cliInput.focus();
    }
  });

  connect();
  els.cliInput.focus();
})();
