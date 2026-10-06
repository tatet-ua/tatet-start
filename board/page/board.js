/* board.js — канбан-доска владельца: одна страница, без сборки и без библиотек.
 *
 * Безопасность: весь текст из данных карточек вставляется только через textContent
 * или узлы, созданные createElement. Опасные для чужого текста способы вставки
 * разметки в этом файле не используются нигде — это можно проверить грепом.
 *
 * Страница — это ВЛАДЕЛЕЦ: роль в запросах не передаётся, её ставит server.py.
 */
(function () {
  "use strict";

  var POLL_VISIBLE = 5000;
  var POLL_HIDDEN = 30000;
  var LS_PROJECT = "board.project";
  var LS_THEME = "board.theme";
  var ALL = "__all__";
  var SESSION_LINK_RE = /^claude:\/\/claude\.ai\/epitaxy\/[A-Za-z0-9_-]+$/;

  var FIELD_LABELS = {
    ask: "Что решить владельцу",
    proof: "Чем доказано",
    accept_how: "Критерий проверки: что выполнить → что должно получиться",
    comment: "Что не так",
  };

  var dom = {};

  var S = {
    data: null,
    token: null,
    project: null,
    archiveOpen: false,
    theme: null,
    dragging: false,
    dragCard: null,
    openDialogKind: null, // 'card' | 'menu' | 'fields' | 'accept' | 'new' | 'edit' | 'confirm' | null
    openCard: null,       // {project, id} — для окна карточки
    pendingRender: false,
    pollTimer: null,
    pollInFlight: false,
    connLost: false,
    lastUpdated: null,
    cardIndex: {},
  };

  // ---------------------------------------------------------------- DOM-хелперы

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function clear(node) {
    node.replaceChildren();
  }

  function mkButton(label, className, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    if (className) b.className = className;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function makeTag(text, cls) {
    return el("span", "tag " + cls, text);
  }

  function closeButton(onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "dlg-close";
    b.setAttribute("aria-label", "Закрыть");
    b.textContent = "×";
    b.addEventListener("click", onClick);
    return b;
  }

  function firstLine(text) {
    if (!text) return "";
    var idx = text.indexOf("\n");
    return idx === -1 ? text : text.slice(0, idx);
  }

  // ---------------------------------------------------------------- дата и время

  function parseLocalDate(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function fmtDT(iso) {
    var d = parseLocalDate(iso);
    if (!d) return "";
    return pad2(d.getDate()) + "." + pad2(d.getMonth() + 1) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function fmtHMS(d) {
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function daysAgoLabel(iso) {
    var d = parseLocalDate(iso);
    if (!d) return "";
    var now = new Date();
    var startOf = function (x) {
      return new Date(x.getFullYear(), x.getMonth(), x.getDate());
    };
    var diffDays = Math.round((startOf(now) - startOf(d)) / 86400000);
    if (diffDays <= 0) return "сегодня";
    if (diffDays === 1) return "вчера";
    return diffDays + " дн. назад";
  }

  // ---------------------------------------------------------------- сеть

  function request(method, path, body) {
    var opts = { method: method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(
      function (res) {
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (json) {
            return { ok: res.ok, status: res.status, data: json };
          });
      },
      function () {
        return { ok: false, status: 0, data: null, networkError: true };
      }
    );
  }

  function errText(res) {
    if (res && res.data && Array.isArray(res.data.error)) return res.data.error.join("\n");
    if (res && res.networkError) return "нет связи с программой";
    return "ошибка " + (res ? res.status : "?");
  }

  // ---------------------------------------------------------------- статусы/роли/подписи

  function statusTitle(key) {
    if (!S.data) return key;
    for (var i = 0; i < S.data.statuses.length; i++) {
      if (S.data.statuses[i].key === key) return S.data.statuses[i].title;
    }
    return key;
  }

  function roleTitle(key) {
    return (S.data && S.data.meta && S.data.meta.roles && S.data.meta.roles[key]) || key;
  }

  function priorityLabel(p) {
    return "приоритет " + p;
  }

  // ---------------------------------------------------------------- тема

  function loadTheme() {
    try {
      S.theme = localStorage.getItem(LS_THEME);
    } catch (e) {
      S.theme = null;
    }
  }

  function applyTheme() {
    if (S.theme === "light" || S.theme === "dark") {
      document.documentElement.setAttribute("data-theme", S.theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    var label = S.theme === "light" ? "Тема: светлая" : S.theme === "dark" ? "Тема: тёмная" : "Тема: системная";
    dom.themeToggle.textContent = label;
  }

  function cycleTheme() {
    var order = [null, "light", "dark"];
    var idx = order.indexOf(S.theme);
    if (idx === -1) idx = 0;
    S.theme = order[(idx + 1) % order.length];
    try {
      if (S.theme) localStorage.setItem(LS_THEME, S.theme);
      else localStorage.removeItem(LS_THEME);
    } catch (e) {
      /* localStorage недоступен — тема просто не переживёт перезагрузку */
    }
    applyTheme();
  }

  // ---------------------------------------------------------------- проект: выбор и адрес

  function initialProjectFromLocation() {
    var hash = location.hash.replace(/^#/, "");
    var fromHash = null;
    try {
      fromHash = new URLSearchParams(hash).get("project");
    } catch (e) {
      fromHash = null;
    }
    if (fromHash) return fromHash;
    try {
      return localStorage.getItem(LS_PROJECT);
    } catch (e) {
      return null;
    }
  }

  function persistProject(name) {
    try {
      localStorage.setItem(LS_PROJECT, name);
    } catch (e) {
      /* ok */
    }
    try {
      var params = new URLSearchParams(location.hash.replace(/^#/, ""));
      params.set("project", name);
      history.replaceState(null, "", "#" + params.toString());
    } catch (e) {
      /* ok */
    }
  }

  function ensureProjectSelection() {
    var names = S.data.projects.map(function (p) {
      return p.project;
    });
    if (S.project === null || S.project === undefined) {
      var wanted = initialProjectFromLocation();
      if (wanted === ALL || (wanted && names.indexOf(wanted) !== -1)) {
        S.project = wanted;
      } else {
        // Без сохранённого выбора и без #project в адресе — утренний вид владельца:
        // «Все проекты», а не первый по алфавиту (правка после ручной проверки, п.2).
        S.project = ALL;
      }
    } else if (S.project !== ALL && names.indexOf(S.project) === -1) {
      S.project = ALL;
    }
  }

  // ---------------------------------------------------------------- toast

  function showToast(message, kind) {
    var t = el("div", "toast " + (kind === "error" ? "toast-error" : "toast-info"), message);
    dom.toastRegion.appendChild(t);
    setTimeout(function () {
      t.remove();
    }, 5000);
  }

  // ---------------------------------------------------------------- буфер обмена

  function copyText(text, btn) {
    var done = false;
    var attemptFallback = function () {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.top = "0";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        var ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
      } catch (e) {
        return false;
      }
    };
    var finish = function (ok) {
      if (!btn) return;
      var original = btn.textContent;
      btn.textContent = ok ? "скопировано" : "не вышло";
      setTimeout(function () {
        btn.textContent = original;
      }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          finish(true);
        },
        function () {
          finish(attemptFallback());
        }
      );
    } else {
      done = attemptFallback();
      finish(done);
    }
  }

  // ---------------------------------------------------------------- опрос состояния

  function scheduleNext() {
    if (S.pollTimer) clearTimeout(S.pollTimer);
    var delay = document.hidden ? POLL_HIDDEN : POLL_VISIBLE;
    S.pollTimer = setTimeout(function () {
      pollOnce(false);
    }, delay);
  }

  function setConnLost(lost) {
    S.connLost = lost;
    renderConnStatus();
  }

  function setLastUpdated(date) {
    S.lastUpdated = date;
    renderConnStatus();
  }

  function renderConnStatus() {
    if (S.connLost) {
      dom.connStatus.textContent = "нет связи с программой";
      dom.connStatus.classList.add("is-offline");
    } else {
      dom.connStatus.classList.remove("is-offline");
      var t = S.lastUpdated ? fmtHMS(S.lastUpdated) : "—";
      dom.connStatus.textContent = "обновлено " + t;
    }
  }

  function isSuspended() {
    return S.dragging || (S.openDialogKind !== null && S.openDialogKind !== "card");
  }

  function applyState(payload) {
    S.data = payload;
    S.token = payload.token;
    setLastUpdated(new Date());
    ensureProjectSelection();
    if (isSuspended()) {
      S.pendingRender = true;
    } else {
      render();
    }
  }

  function pollOnce(forceFull) {
    if (S.pollInFlight) return Promise.resolve();
    S.pollInFlight = true;
    var path = "/api/state";
    if (!forceFull && S.token) path += "?since=" + encodeURIComponent(S.token);
    return request("GET", path).then(function (res) {
      if (!res.ok) {
        setConnLost(true);
      } else {
        setConnLost(false);
        if (res.data && res.data.unchanged) {
          setLastUpdated(new Date());
        } else if (res.data) {
          applyState(res.data);
        }
      }
      S.pollInFlight = false;
      scheduleNext();
    });
  }

  function onVisibilityChange() {
    if (document.hidden) {
      if (S.pollTimer) clearTimeout(S.pollTimer);
      S.pollTimer = setTimeout(function () {
        pollOnce(false);
      }, POLL_HIDDEN);
    } else {
      if (S.pollTimer) clearTimeout(S.pollTimer);
      pollOnce(false);
    }
  }

  function flushPendingRenderMaybe() {
    if (!S.dragging && !S.openDialogKind && S.pendingRender) {
      S.pendingRender = false;
      render();
    }
  }

  // ---------------------------------------------------------------- диалоги: общее

  // Правка после ручной проверки (п.3, п.10): HTMLDialogElement.close() ставит .open в
  // false сразу, но само событие 'close' браузер ставит в очередь и стреляет им ПОЗЖЕ —
  // асинхронно. Наш же код в ответ на «Переместить в…»/«Править»/«Удалить» закрывает
  // окно карточки и в ТОМ ЖЕ обработчике сразу открывает следующий диалог. Пока
  // отложенное 'close' от закрытия окна карточки наконец происходит, счётчик
  // S.openDialogKind уже может показывать другой (или тот же переоткрытый) диалог —
  // старый обработчик наивно писал в него null и стирал S.openCard, из-за чего окно
  // карточки переставало обновляться из опроса насовсем. Поэтому: 1) свои закрытия
  // (кнопка, «Отмена», подложка) идут через closeDialogEl(), который правит состояние
  // СИНХРОННО, не дожидаясь события; 2) обработчик 'close' на диалоге — только сетка
  // для Esc/сворачивания браузером, и он же проверяет двумя способами, что событие не
  // устарело: диалог до сих пор закрыт (dlg.open) и текущий отслеживаемый диалог — это
  // именно он (S.openDialogKind === kind), а не что-то, открытое уже после.

  function kindForDialog(dlgEl) {
    if (dlgEl === dom.cardDialog) return "card";
    if (dlgEl === dom.menuDialog) return "menu";
    if (dlgEl === dom.fieldsDialog) return "fields";
    if (dlgEl === dom.acceptDialog) return "accept";
    if (dlgEl === dom.newCardDialog) return "new";
    if (dlgEl === dom.editDialog) return "edit";
    if (dlgEl === dom.confirmDialog) return "confirm";
    return null;
  }

  function closeDialogEl(dlgEl) {
    if (!dlgEl.open) return;
    dlgEl.close();
    var kind = kindForDialog(dlgEl);
    if (S.openDialogKind === kind) {
      S.openDialogKind = null;
      if (kind === "card") S.openCard = null;
    }
    flushPendingRenderMaybe();
  }

  function setOpenDialog(kind) {
    S.openDialogKind = kind;
  }

  // ---------------------------------------------------------------- скролл

  function captureScroll() {
    return {
      win: window.scrollY,
      board: dom.board.scrollLeft,
      archive: dom.archiveBoard.scrollLeft,
    };
  }

  function restoreScroll(s) {
    dom.board.scrollLeft = s.board;
    dom.archiveBoard.scrollLeft = s.archive;
    window.scrollTo(0, s.win);
  }

  // ---------------------------------------------------------------- индекс карточек

  function buildCardIndex() {
    var idx = {};
    for (var i = 0; i < S.data.projects.length; i++) {
      var p = S.data.projects[i];
      for (var j = 0; j < p.cards.length; j++) {
        idx[p.project + "|" + p.cards[j].id] = p.cards[j];
      }
    }
    S.cardIndex = idx;
  }

  function visibleCards() {
    var out = [];
    for (var i = 0; i < S.data.projects.length; i++) {
      var p = S.data.projects[i];
      if (S.project !== ALL && p.project !== S.project) continue;
      for (var j = 0; j < p.cards.length; j++) out.push({ project: p.project, card: p.cards[j] });
    }
    return out;
  }

  // ---------------------------------------------------------------- рендер: корень

  function render() {
    if (!S.data) return;
    var scroll = captureScroll();
    buildCardIndex();
    renderProjectOptions();
    renderSummary();
    renderBoard();
    renderArchive();
    restoreScroll(scroll);
    if (S.openDialogKind === "card" && S.openCard) {
      var card = S.cardIndex[S.openCard.project + "|" + S.openCard.id];
      if (card) {
        renderCardDialogContent(card, S.openCard.project);
      } else {
        closeDialogEl(dom.cardDialog);
        showToast("Карточка больше не существует — доска обновлена.", "info");
      }
    }
  }

  function renderProjectOptions() {
    var select = dom.projectSelect;
    var wanted = S.project;
    clear(select);
    var allOpt = document.createElement("option");
    allOpt.value = ALL;
    allOpt.textContent = "Все проекты";
    select.appendChild(allOpt);
    for (var i = 0; i < S.data.projects.length; i++) {
      var p = S.data.projects[i];
      var opt = document.createElement("option");
      opt.value = p.project;
      opt.textContent = p.project;
      select.appendChild(opt);
    }
    select.value = wanted;
    if (select.value !== wanted) {
      select.value = ALL;
      S.project = ALL;
    }
  }

  // ---------------------------------------------------------------- сводка «все проекты»

  function sectionBase(title, cls) {
    var sec = el("section", "panel summary-section" + (cls ? " " + cls : ""));
    sec.appendChild(el("h2", "summary-title", title));
    return sec;
  }

  function renderSummary() {
    var show = S.project === ALL;
    dom.summary.hidden = !show;
    clear(dom.summary);
    if (!show || !S.data.summary) return;
    var summary = S.data.summary;
    if (summary.broken && summary.broken.length) {
      dom.summary.appendChild(renderBrokenSection(summary.broken));
    }
    dom.summary.appendChild(renderWaitingSection(summary.waiting || []));
    dom.summary.appendChild(renderUnfinishedSection(summary.unfinished || []));
    dom.summary.appendChild(renderDoneYesterdaySection(summary.done_yesterday || []));
    if (summary.sessions_without_card && summary.sessions_without_card.length) {
      dom.summary.appendChild(renderSessionsWithoutCardSection(summary.sessions_without_card));
    }
  }

  function renderBrokenSection(list) {
    var sec = sectionBase("Повреждённые файлы проектов", "summary-broken");
    var ul = el("ul", "summary-list");
    for (var i = 0; i < list.length; i++) {
      var li = document.createElement("li");
      var p = el("p", "summary-row", list[i].project + ": " + list[i].error);
      p.style.cursor = "default";
      li.appendChild(p);
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    return sec;
  }

  // Метки приёмки (спека 2026-09-29, пункт 13): «принимаю сам» у owner, у reviewer — ничего
  // (умолчание не подписывается); в «Завершена» — кто принял, по последней записи истории.
  function acceptanceTag(card) {
    return card.acceptance === "owner" ? makeTag("принимаю сам", "tag-acceptance") : null;
  }

  function acceptedBy(card) {
    if (card.status !== "accepted" || !card.history) return null;
    for (var i = card.history.length - 1; i >= 0; i--) {
      if (card.history[i].to === "accepted") return card.history[i].who || null;
    }
    return null;
  }

  function acceptedTag(card) {
    var who = acceptedBy(card);
    if (!who) return null;
    return makeTag(who === "reviewer" ? "принято агентом" : "принято владельцем", "tag-accepted");
  }

  function renderWaitingSection(list) {
    var sec = sectionBase("Ждёт тебя");
    var pending = (S.data.summary && S.data.summary.verify_pending) || 0;
    if (!list.length) {
      sec.appendChild(el("p", "summary-empty", "никто не ждёт"));
      sec.appendChild(el("p", "summary-empty", "Ждёт проверки агентом: " + pending));
      return sec;
    }
    var ul = el("ul", "summary-list");
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "summary-row";
      var head = el("div", "sr-head");
      head.appendChild(el("span", "sr-id mono", item.project + " · " + item.id));
      head.appendChild(el("span", "sr-title", item.title));
      var acc = acceptanceTag(item);
      if (acc) head.appendChild(acc);
      btn.appendChild(head);
      var line = statusTitle(item.status);
      var extra = item.status === "review" ? item.ask : item.accept_how;
      if (extra) line += " · " + firstLine(extra);
      btn.appendChild(el("p", "sr-line", line));
      (function (proj, id) {
        btn.addEventListener("click", function () {
          openCardDialog(proj, id);
        });
      })(item.project, item.id);
      li.appendChild(btn);
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    sec.appendChild(el("p", "summary-empty", "Ждёт проверки агентом: " + pending));
    return sec;
  }

  function renderUnfinishedSection(list) {
    var sec = sectionBase("Не закончено");
    if (!list.length) {
      sec.appendChild(el("p", "summary-empty", "всё закончено"));
      return sec;
    }
    var ul = el("ul", "summary-list");
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "summary-row" + (item.stale ? " is-stale" : "");
      var head = el("div", "sr-head");
      head.appendChild(el("span", "sr-id mono", item.project + " · " + item.id));
      head.appendChild(el("span", "sr-title", item.title));
      // Правка после ручной проверки (п.6): «на доработку» — главное, что владелец и
      // агент должны увидеть утром, не открывая карточку.
      if (item.rework) head.appendChild(makeTag("на доработку", "tag-rework"));
      head.appendChild(el("span", "sr-line", daysAgoLabel(item.touched_at)));
      btn.appendChild(head);
      if (item.rework) btn.appendChild(el("p", "sr-line sr-line-rework", firstLine(item.rework)));
      if (item.stopped_at) btn.appendChild(el("p", "sr-line", "на чём остановились: " + item.stopped_at));
      (function (proj, id) {
        btn.addEventListener("click", function () {
          openCardDialog(proj, id);
        });
      })(item.project, item.id);
      li.appendChild(btn);
      if (item.resume) {
        (function (text) {
          var copyBtn = mkButton("Скопировать запрос", "btn btn-small btn-ghost", function (ev) {
            ev.stopPropagation();
            copyText(text, copyBtn);
          });
          li.appendChild(copyBtn);
        })(item.resume);
      }
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    return sec;
  }

  function renderDoneYesterdaySection(list) {
    var sec = sectionBase("Сделано вчера");
    if (!list.length) {
      sec.appendChild(el("p", "summary-empty", "вчера ничего не закрыто"));
      return sec;
    }
    var ul = el("ul", "summary-list");
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "summary-row";
      // Правка после ручной проверки (п.7): тот же вид, что у остальных списков сводки —
      // проект и id моноширинным, заголовок отдельно, статус меткой, а не сплошной строкой.
      var head = el("div", "sr-head");
      head.appendChild(el("span", "sr-id mono", item.project + " · " + item.id));
      head.appendChild(el("span", "sr-title", item.title));
      head.appendChild(makeTag(statusTitle(item.reached), "tag-acceptance"));
      if (item.caveat) head.appendChild(makeTag("с оговоркой", "tag-caveat"));
      btn.appendChild(head);
      (function (proj, id) {
        btn.addEventListener("click", function () {
          openCardDialog(proj, id);
        });
      })(item.project, item.id);
      li.appendChild(btn);
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    return sec;
  }

  function renderSessionsWithoutCardSection(list) {
    var sec = sectionBase("Сессии без карточки");
    var ul = el("ul", "summary-list");
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      var li = document.createElement("li");
      var text = (item && (item.title || item.session || item.project)) || "сессия";
      li.appendChild(el("p", "summary-row", String(text)));
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    return sec;
  }

  // ---------------------------------------------------------------- доска: колонки и карточки

  function spineFor(card) {
    if (card.rework) return "danger";
    if (card.status === "review") return "info";
    if (card.status === "done") return "warning";
    if (card.status === "accepted") return "success";
    if (card.status === "postponed" || card.status === "cancelled") return "neutral";
    return null;
  }

  function renderBoard() {
    clear(dom.board);
    var columns = S.data.statuses.slice(0, 6);
    var items = visibleCards();
    for (var i = 0; i < columns.length; i++) {
      var col = columns[i];
      var list = items.filter(function (it) {
        return it.card.status === col.key;
      });
      dom.board.appendChild(renderColumn(col, list));
    }
  }

  function renderArchive() {
    clear(dom.archiveBoard);
    if (!S.data) return;
    var archiveStatuses = S.data.statuses.slice(6);
    var items = visibleCards();
    for (var i = 0; i < archiveStatuses.length; i++) {
      var col = archiveStatuses[i];
      var list = items.filter(function (it) {
        return it.card.status === col.key;
      });
      dom.archiveBoard.appendChild(renderColumn(col, list));
    }
    dom.archiveWrap.hidden = !S.archiveOpen;
  }

  function renderColumn(colMeta, list) {
    var column = el("section", "column");
    column.dataset.status = colMeta.key;
    var head = el("div", "column-head");
    head.appendChild(el("h2", "column-title", colMeta.title));
    head.appendChild(el("span", "column-count", String(list.length)));
    var body = el("div", "column-body");
    body.dataset.status = colMeta.key;
    if (list.length === 0) {
      body.appendChild(el("p", "column-empty", "пусто"));
    } else {
      for (var i = 0; i < list.length; i++) {
        body.appendChild(renderCard(list[i].project, list[i].card));
      }
    }
    column.appendChild(head);
    column.appendChild(body);
    attachDropZone(body, colMeta.key);
    return column;
  }

  function renderCard(project, card) {
    var wrap = el("article", "card");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "button");
    wrap.setAttribute("aria-label", "Открыть карточку " + card.id + ": " + card.title);
    wrap.draggable = true;
    wrap.dataset.project = project;
    wrap.dataset.id = card.id;
    var spine = spineFor(card);
    if (spine) wrap.dataset.spine = spine;

    // Правка после ручной проверки (п.4): метка проекта рядом с id переносила id
    // посреди слова («nuxt-templat / e-1») в режиме «Все проекты». Имя проекта и так
    // есть в самом id (<проект>-<номер>), отдельная метка убрана; id теперь не
    // переносится, а обрезается многоточием с полным значением в title.
    var top = el("div", "card-top");
    var idSpan = el("span", "card-id mono", card.id);
    idSpan.title = card.id;
    top.appendChild(idSpan);
    wrap.appendChild(top);

    wrap.appendChild(el("p", "card-title", card.title));

    var labels = el("div", "card-labels");
    if (card.kind === "stage" && card.stage) labels.appendChild(makeTag("этап " + card.stage, "tag-stage"));
    labels.appendChild(makeTag(priorityLabel(card.priority), "tag-priority"));
    if (card.tag) {
      var tagWord = (S.data.meta.tags && S.data.meta.tags[card.tag]) || card.tag;
      labels.appendChild(makeTag(tagWord, card.tag === "urgent" ? "tag-urgent" : "tag-block"));
    }
    var accTag = acceptanceTag(card);
    if (accTag) labels.appendChild(accTag);
    var byTag = acceptedTag(card);
    if (byTag) labels.appendChild(byTag);
    if (card.caveat) labels.appendChild(makeTag("с оговоркой", "tag-caveat"));
    wrap.appendChild(labels);

    if (card.rework) {
      var rw = el("div", "card-rework");
      rw.appendChild(el("b", null, "на доработку"));
      rw.appendChild(el("p", null, firstLine(card.rework)));
      wrap.appendChild(rw);
    }

    var actions = el("div", "card-actions");
    actions.appendChild(
      // Правка после ручной проверки (п.5): «Переместить в…» не помещалось в карточке
      // на 1280 px в шести колонках — короче подпись, диалог сам называет колонки.
      mkButton("Переместить", "btn btn-small btn-ghost", function (ev) {
        ev.stopPropagation();
        openMoveMenu(project, card, null);
      })
    );
    wrap.appendChild(actions);

    wrap.addEventListener("click", function () {
      openCardDialog(project, card.id);
    });
    wrap.addEventListener("keydown", function (ev) {
      if (ev.target !== wrap) return;
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
        ev.preventDefault();
        openCardDialog(project, card.id);
      }
    });
    wrap.addEventListener("dragstart", function (ev) {
      S.dragging = true;
      S.dragCard = { project: project, id: card.id };
      wrap.classList.add("is-dragging");
      try {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", project + "|" + card.id);
      } catch (e) {
        /* некоторые браузеры капризничают с dataTransfer — не критично */
      }
    });
    wrap.addEventListener("dragend", function () {
      S.dragging = false;
      S.dragCard = null;
      wrap.classList.remove("is-dragging");
      flushPendingRenderMaybe();
    });

    return wrap;
  }

  function attachDropZone(bodyEl, statusKey) {
    bodyEl.addEventListener("dragover", function (ev) {
      if (!S.dragCard) return;
      ev.preventDefault();
      try {
        ev.dataTransfer.dropEffect = "move";
      } catch (e) {
        /* ok */
      }
      bodyEl.closest(".column").classList.add("is-drop-target");
    });
    bodyEl.addEventListener("dragleave", function () {
      bodyEl.closest(".column").classList.remove("is-drop-target");
    });
    bodyEl.addEventListener("drop", function (ev) {
      ev.preventDefault();
      bodyEl.closest(".column").classList.remove("is-drop-target");
      var dragCard = S.dragCard;
      if (!dragCard) return;
      var card = S.cardIndex[dragCard.project + "|" + dragCard.id];
      if (!card) return;
      if (card.status === statusKey) return;
      attemptMove(dragCard.project, card, statusKey, null);
    });
  }

  // ---------------------------------------------------------------- перенос: can-move → move

  function doMove(project, card, toStatus, extra) {
    var payload = { to: toStatus, version: card.version };
    for (var k in extra) if (Object.prototype.hasOwnProperty.call(extra, k)) payload[k] = extra[k];
    return request("POST", "/api/cards/" + encodeURIComponent(card.id) + "/move", payload).then(function (res) {
      if (res.ok) {
        return pollOnce(true).then(function () {
          return { ok: true };
        });
      }
      if (res.status === 409) {
        showToast("карточку уже изменили, доска обновлена", "error");
        return pollOnce(true).then(function () {
          return { ok: false, conflict: true };
        });
      }
      return { ok: false, error: errText(res) };
    });
  }

  function attemptMove(project, card, toStatus, returnTo) {
    var path = "/api/cards/" + encodeURIComponent(card.id) + "/can-move?to=" + encodeURIComponent(toStatus);
    return request("GET", path).then(function (res) {
      if (!res.ok) {
        showToast(errText(res), "error");
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
        return;
      }
      var allowed = res.data.allowed;
      var reason = res.data.reason;
      var required = res.data.required || [];
      var optional = res.data.optional || [];
      if (!allowed) {
        showToast(reason || "Перенос недоступен.", "error");
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
        return;
      }
      if (required.length) {
        openFieldsDialog(project, card, toStatus, required, returnTo);
        return;
      }
      if (optional.indexOf("caveat") !== -1) {
        openAcceptDialog(project, card, toStatus, returnTo);
        return;
      }
      return doMove(project, card, toStatus, {}).then(function (result) {
        if (!result.ok && !result.conflict && result.error) showToast(result.error, "error");
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      });
    });
  }

  // ---------------------------------------------------------------- окно карточки

  function openCardDialog(project, id) {
    var card = S.cardIndex[project + "|" + id];
    if (!card) {
      showToast("Карточка не найдена — доска обновлена.", "error");
      pollOnce(true);
      return;
    }
    S.openCard = { project: project, id: id };
    setOpenDialog("card");
    renderCardDialogContent(card, project);
    if (!dom.cardDialog.open) dom.cardDialog.showModal();
  }

  function dlgSection(titleText) {
    var sec = el("div", "dlg-section");
    if (titleText) sec.appendChild(el("p", "dlg-section-title", titleText));
    return sec;
  }

  function renderCardDialogContent(card, project) {
    var dlg = dom.cardDialog;

    // сохраняем черновик комментария и фокус, чтобы опрос не стирал набранный текст
    var draft = "";
    var hadFocus = false;
    var existing = dlg.querySelector("#comment-input");
    if (existing) {
      draft = existing.value;
      hadFocus = document.activeElement === existing;
    }

    clear(dlg);

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, card.id + " — " + card.title));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");

    var labels = el("div", "card-labels");
    if (card.kind === "stage" && card.stage) labels.appendChild(makeTag("этап " + card.stage, "tag-stage"));
    labels.appendChild(makeTag(priorityLabel(card.priority), "tag-priority"));
    if (card.tag) {
      var tagWord = (S.data.meta.tags && S.data.meta.tags[card.tag]) || card.tag;
      labels.appendChild(makeTag(tagWord, card.tag === "urgent" ? "tag-urgent" : "tag-block"));
    }
    if (S.project === ALL) labels.appendChild(makeTag(project, "tag-project"));
    labels.appendChild(makeTag(statusTitle(card.status), "tag-acceptance"));
    var dlgAccTag = acceptanceTag(card);
    if (dlgAccTag) labels.appendChild(dlgAccTag);
    var dlgByTag = acceptedTag(card);
    if (dlgByTag) labels.appendChild(dlgByTag);
    if (card.caveat) labels.appendChild(makeTag("с оговоркой", "tag-caveat"));
    body.appendChild(labels);

    if (card.rework) {
      var rw = el("div", "card-rework");
      rw.appendChild(el("b", null, "На доработку"));
      rw.appendChild(el("p", null, card.rework));
      body.appendChild(rw);
    }

    if (card.body) {
      var secBody = dlgSection("Текст");
      secBody.appendChild(el("p", null, card.body));
      body.appendChild(secBody);
    }

    if (card.status === "review" && card.ask) {
      var secAsk = dlgSection("Что решить");
      secAsk.appendChild(el("p", null, card.ask));
      body.appendChild(secAsk);
    }

    if (card.accept_how) {
      var secAccept = dlgSection("Критерий проверки");
      secAccept.appendChild(el("p", null, card.accept_how));
      body.appendChild(secAccept);
    }

    if (card.proof) {
      var secProof = dlgSection("Чем доказано");
      secProof.appendChild(el("p", null, card.proof));
      body.appendChild(secProof);
    }

    // Раздел «Проверка» (спека 2026-09-29, пункт 13): команда, дата, вердикт, вывод свёрнут.
    if (card.verified && card.verified.command) {
      var v = card.verified;
      var secVerified = dlgSection("Проверка");
      var verdictWord = v.verdict === "ok" ? "совпало" : "не совпало";
      secVerified.appendChild(el("p", null, roleTitle(v.who) + " · " + fmtDT(v.at) + " · " + verdictWord));
      var cmdP = el("p", "mono", v.command);
      secVerified.appendChild(cmdP);
      var details = document.createElement("details");
      var summaryEl = document.createElement("summary");
      summaryEl.textContent = "показать вывод" + (v.truncated ? " (обрезан)" : "");
      details.appendChild(summaryEl);
      details.appendChild(el("pre", "verify-output", v.output || ""));
      secVerified.appendChild(details);
      body.appendChild(secVerified);
    }

    if (card.caveat) {
      var secCaveat = dlgSection(null);
      secCaveat.appendChild(el("p", null, "С оговоркой: " + card.caveat));
      body.appendChild(secCaveat);
    }

    if (card.stopped_at || card.resume) {
      var secStop = dlgSection("На чём остановились");
      if (card.stopped_at) secStop.appendChild(el("p", null, card.stopped_at));
      if (card.resume) {
        var row = el("div", "session-row");
        var copyReqBtn = mkButton("Скопировать запрос", "btn btn-small", function () {
          copyText(card.resume, copyReqBtn);
        });
        row.appendChild(copyReqBtn);
        secStop.appendChild(row);
      }
      body.appendChild(secStop);
    }

    var secSession = dlgSection("Сессия");
    if (card.session && (card.session.title || card.session.link)) {
      var srow = el("div", "session-row");
      srow.appendChild(el("span", null, card.session.title || "(без названия)"));
      var link = card.session.link || "";
      if (link && SESSION_LINK_RE.test(link)) {
        var a = document.createElement("a");
        a.href = link;
        a.className = "btn btn-small";
        a.textContent = "Открыть сессию";
        srow.appendChild(a);
      } else if (link) {
        srow.appendChild(el("span", "mono", link));
      }
      if (link) {
        var copyLinkBtn = mkButton("Скопировать ссылку", "btn btn-small btn-ghost", function () {
          copyText(link, copyLinkBtn);
        });
        srow.appendChild(copyLinkBtn);
      }
      secSession.appendChild(srow);
    } else {
      secSession.appendChild(el("p", "summary-empty", "сессия не привязана"));
    }
    body.appendChild(secSession);

    var secActions = dlgSection(null);
    var actionsRow = el("div", "session-row");
    actionsRow.appendChild(
      mkButton("Переместить в…", "btn btn-small", function () {
        var returnTo = { project: project, id: card.id };
        closeDialogEl(dlg);
        openMoveMenu(project, card, returnTo);
      })
    );
    actionsRow.appendChild(
      mkButton("Править", "btn btn-small", function () {
        var returnTo = { project: project, id: card.id };
        closeDialogEl(dlg);
        openEditDialog(project, card, returnTo);
      })
    );
    actionsRow.appendChild(
      mkButton("Удалить", "btn btn-small btn-danger", function () {
        var returnTo = { project: project, id: card.id };
        closeDialogEl(dlg);
        openDeleteConfirm(project, card, returnTo);
      })
    );
    secActions.appendChild(actionsRow);
    body.appendChild(secActions);

    var secComments = dlgSection("Комментарии");
    if (card.comments && card.comments.length) {
      var ulc = el("ul", "feed");
      for (var i = 0; i < card.comments.length; i++) {
        var c = card.comments[i];
        var li = document.createElement("li");
        var k = el("span", "k", roleTitle(c.who));
        k.appendChild(el("small", null, fmtDT(c.at)));
        var em = el("em", null, c.text);
        li.appendChild(k);
        li.appendChild(em);
        ulc.appendChild(li);
      }
      secComments.appendChild(ulc);
    } else {
      secComments.appendChild(el("p", "summary-empty", "комментариев ещё нет"));
    }
    // Правка после ручной проверки (п.8): «Отправить» — не главное действие окна, синяя
    // кнопка во всю ширину привлекала внимание больше, чем «Переместить в…»/«Принять».
    // Поле формы (label+textarea) остаётся в своей строке-колонке, а кнопка вынесена в
    // отдельный ряд с justify-content:flex-end — обычная вторичная кнопка по правому краю.
    var commentForm = document.createElement("form");
    commentForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
    });
    var commentFieldRow = el("div", "dlg-form-row");
    var commentLabel = document.createElement("label");
    commentLabel.setAttribute("for", "comment-input");
    commentLabel.textContent = "Написать комментарий";
    var commentInput = document.createElement("textarea");
    commentInput.id = "comment-input";
    commentInput.value = draft;
    commentFieldRow.appendChild(commentLabel);
    commentFieldRow.appendChild(commentInput);
    commentForm.appendChild(commentFieldRow);
    var commentError = el("p", "dlg-error");
    commentForm.appendChild(commentError);
    var commentActions = el("div", "dlg-form-actions");
    var commentSubmit = mkButton("Отправить", "btn btn-small", function () {
      var text = commentInput.value.trim();
      if (!text) return;
      commentSubmit.disabled = true;
      request("POST", "/api/cards/" + encodeURIComponent(card.id) + "/comment", { text: text, version: card.version }).then(function (res) {
        commentSubmit.disabled = false;
        if (res.ok) {
          pollOnce(true);
        } else if (res.status === 409) {
          showToast("карточку уже изменили, доска обновлена", "error");
          pollOnce(true);
        } else {
          commentError.textContent = errText(res);
        }
      });
    });
    commentActions.appendChild(commentSubmit);
    commentForm.appendChild(commentActions);
    secComments.appendChild(commentForm);
    body.appendChild(secComments);

    var secHistory = dlgSection("История");
    if (card.history && card.history.length) {
      var ulh = el("ul", "feed");
      for (var j = 0; j < card.history.length; j++) {
        var h = card.history[j];
        var lih = document.createElement("li");
        var kh = el("span", "k", roleTitle(h.who));
        kh.appendChild(el("small", null, fmtDT(h.at)));
        var fromT = h.from ? statusTitle(h.from) : "(создание)";
        var toT = statusTitle(h.to);
        var emh = el("em", null, fromT + " → " + toT + (h.text ? " — " + h.text : ""));
        lih.appendChild(kh);
        lih.appendChild(emh);
        ulh.appendChild(lih);
      }
      secHistory.appendChild(ulh);
    } else {
      secHistory.appendChild(el("p", "summary-empty", "переходов ещё не было"));
    }
    body.appendChild(secHistory);

    dlg.appendChild(body);

    if (hadFocus) {
      commentInput.focus();
      var len = commentInput.value.length;
      try {
        commentInput.setSelectionRange(len, len);
      } catch (e) {
        /* ok */
      }
    }
  }

  // ---------------------------------------------------------------- «Переместить в…»

  function openMoveMenu(project, card, returnTo) {
    var dlg = dom.menuDialog;
    clear(dlg);
    setOpenDialog("menu");

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, "Переместить " + card.id));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");
    var ul = el("ul", "dlg-menu");
    for (var i = 0; i < S.data.statuses.length; i++) {
      var col = S.data.statuses[i];
      if (col.key === card.status) continue;
      var li = document.createElement("li");
      (function (targetKey, targetTitle) {
        var btn = mkButton(targetTitle, "btn", function () {
          closeDialogEl(dlg);
          attemptMove(project, card, targetKey, returnTo);
        });
        li.appendChild(btn);
      })(col.key, col.title);
      ul.appendChild(li);
    }
    body.appendChild(ul);
    dlg.appendChild(body);

    var foot = el("div", "dlg-foot");
    foot.appendChild(
      mkButton("Отмена", "btn btn-ghost", function () {
        closeDialogEl(dlg);
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      })
    );
    dlg.appendChild(foot);

    dlg.showModal();
  }

  // ---------------------------------------------------------------- обязательные поля перед переносом

  function openFieldsDialog(project, card, toStatus, required, returnTo) {
    var dlg = dom.fieldsDialog;
    clear(dlg);
    setOpenDialog("fields");

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, "Переход в «" + statusTitle(toStatus) + "»"));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");
    var form = document.createElement("form");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
    });
    var inputs = {};
    for (var i = 0; i < required.length; i++) {
      var field = required[i];
      var row = el("div", "dlg-form-row");
      var inputId = "field-" + field;
      var label = document.createElement("label");
      label.setAttribute("for", inputId);
      label.textContent = FIELD_LABELS[field] || field;
      var textarea = document.createElement("textarea");
      textarea.id = inputId;
      textarea.required = true;
      var prefill = card[field] || "";
      if (field === "proof" && card.rework) prefill = "";
      if (field === "comment") prefill = "";
      textarea.value = prefill;
      row.appendChild(label);
      row.appendChild(textarea);
      form.appendChild(row);
      inputs[field] = textarea;
    }
    var errorP = el("p", "dlg-error");
    form.appendChild(errorP);
    body.appendChild(form);
    dlg.appendChild(body);

    var foot = el("div", "dlg-foot");
    var cancelBtn = mkButton("Отмена", "btn btn-ghost", function () {
      closeDialogEl(dlg);
      if (returnTo) openCardDialog(returnTo.project, returnTo.id);
    });
    var submitBtn = mkButton("Сохранить и перенести", "btn btn-primary", function () {
      var extra = {};
      var missing = false;
      for (var f = 0; f < required.length; f++) {
        var key = required[f];
        var val = inputs[key].value.trim();
        if (!val) missing = true;
        extra[key] = val;
      }
      if (missing) {
        errorP.textContent = "Заполни все поля.";
        return;
      }
      submitBtn.disabled = true;
      doMove(project, card, toStatus, extra).then(function (result) {
        submitBtn.disabled = false;
        if (result.ok || result.conflict) {
          closeDialogEl(dlg);
          if (returnTo) openCardDialog(returnTo.project, returnTo.id);
        } else {
          errorP.textContent = result.error || "";
        }
      });
    });
    foot.appendChild(cancelBtn);
    foot.appendChild(submitBtn);
    dlg.appendChild(foot);

    dlg.showModal();
  }

  // ---------------------------------------------------------------- приёмка с оговоркой

  function openAcceptDialog(project, card, toStatus, returnTo) {
    var dlg = dom.acceptDialog;
    clear(dlg);
    setOpenDialog("accept");

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, "Принять " + card.id));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");
    var row = el("div", "dlg-form-row");
    var label = document.createElement("label");
    label.setAttribute("for", "caveat-input");
    label.textContent = "Оговорка (необязательно)";
    var textarea = document.createElement("textarea");
    textarea.id = "caveat-input";
    row.appendChild(label);
    row.appendChild(textarea);
    body.appendChild(row);
    var errorP = el("p", "dlg-error");
    body.appendChild(errorP);
    dlg.appendChild(body);

    var foot = el("div", "dlg-foot");
    var cancelBtn = mkButton("Отмена", "btn btn-ghost", function () {
      closeDialogEl(dlg);
      if (returnTo) openCardDialog(returnTo.project, returnTo.id);
    });
    var acceptBtn = mkButton("Принять", "btn btn-primary", function () {
      acceptBtn.disabled = true;
      doMove(project, card, toStatus, { caveat: textarea.value.trim() }).then(function (result) {
        acceptBtn.disabled = false;
        if (result.ok || result.conflict) {
          closeDialogEl(dlg);
          if (returnTo) openCardDialog(returnTo.project, returnTo.id);
        } else {
          errorP.textContent = result.error || "";
        }
      });
    });
    foot.appendChild(cancelBtn);
    foot.appendChild(acceptBtn);
    dlg.appendChild(foot);

    dlg.showModal();
  }

  // ---------------------------------------------------------------- правка карточки

  function formRow(labelText, inputEl) {
    var row = el("div", "dlg-form-row");
    var label = document.createElement("label");
    label.textContent = labelText;
    row.appendChild(label);
    row.appendChild(inputEl);
    return row;
  }

  function fillOptions(select, dict, selectedKey, prefix) {
    for (var key in dict) {
      if (!Object.prototype.hasOwnProperty.call(dict, key)) continue;
      var opt = document.createElement("option");
      opt.value = key;
      opt.textContent = (prefix ? key + " · " : "") + dict[key];
      if (key === selectedKey) opt.selected = true;
      select.appendChild(opt);
    }
  }

  function openEditDialog(project, card, returnTo) {
    var dlg = dom.editDialog;
    clear(dlg);
    setOpenDialog("edit");

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, "Править " + card.id));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");
    var form = document.createElement("form");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
    });

    var titleInput = document.createElement("input");
    titleInput.type = "text";
    titleInput.value = card.title;
    titleInput.required = true;

    var bodyInput = document.createElement("textarea");
    bodyInput.value = card.body || "";

    var prioritySelect = document.createElement("select");
    fillOptions(prioritySelect, S.data.meta.priorities, card.priority, true);

    var tagSelect = document.createElement("select");
    var noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "без метки";
    tagSelect.appendChild(noneOpt);
    fillOptions(tagSelect, S.data.meta.tags, card.tag, false);

    var acceptanceSelect = document.createElement("select");
    fillOptions(acceptanceSelect, S.data.meta.acceptance, card.acceptance, false);

    form.appendChild(formRow("Заголовок", titleInput));
    form.appendChild(formRow("Текст", bodyInput));
    form.appendChild(formRow("Приоритет", prioritySelect));
    form.appendChild(formRow("Метка", tagSelect));
    form.appendChild(formRow("Кто принимает", acceptanceSelect));
    var errorP = el("p", "dlg-error");
    form.appendChild(errorP);
    body.appendChild(form);
    dlg.appendChild(body);

    var foot = el("div", "dlg-foot");
    var cancelBtn = mkButton("Отмена", "btn btn-ghost", function () {
      closeDialogEl(dlg);
      if (returnTo) openCardDialog(returnTo.project, returnTo.id);
    });
    var saveBtn = mkButton("Сохранить", "btn btn-primary", function () {
      var title = titleInput.value.trim();
      if (!title) {
        errorP.textContent = "Заголовок обязателен.";
        return;
      }
      var payload = {
        version: card.version,
        title: title,
        body: bodyInput.value,
        priority: prioritySelect.value,
        tag: tagSelect.value || null,
        acceptance: acceptanceSelect.value,
      };
      saveBtn.disabled = true;
      request("POST", "/api/cards/" + encodeURIComponent(card.id) + "/edit", payload).then(function (res) {
        saveBtn.disabled = false;
        if (res.ok) {
          closeDialogEl(dlg);
          pollOnce(true).then(function () {
            if (returnTo) openCardDialog(returnTo.project, returnTo.id);
          });
        } else if (res.status === 409) {
          showToast("карточку уже изменили, доска обновлена", "error");
          closeDialogEl(dlg);
          pollOnce(true).then(function () {
            if (returnTo) openCardDialog(returnTo.project, returnTo.id);
          });
        } else {
          errorP.textContent = errText(res);
        }
      });
    });
    foot.appendChild(cancelBtn);
    foot.appendChild(saveBtn);
    dlg.appendChild(foot);

    dlg.showModal();
  }

  // ---------------------------------------------------------------- удаление

  function openDeleteConfirm(project, card, returnTo) {
    var dlg = dom.confirmDialog;
    clear(dlg);
    setOpenDialog("confirm");

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, "Удалить " + card.id + "?"));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
        if (returnTo) openCardDialog(returnTo.project, returnTo.id);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");
    body.appendChild(el("p", null, "Номер карточки больше не используется."));
    var errorP = el("p", "dlg-error");
    body.appendChild(errorP);
    dlg.appendChild(body);

    var foot = el("div", "dlg-foot");
    var cancelBtn = mkButton("Отмена", "btn btn-ghost", function () {
      closeDialogEl(dlg);
      if (returnTo) openCardDialog(returnTo.project, returnTo.id);
    });
    var deleteBtn = mkButton("Удалить", "btn btn-danger", function () {
      deleteBtn.disabled = true;
      request("POST", "/api/cards/" + encodeURIComponent(card.id) + "/delete", { version: card.version }).then(function (res) {
        deleteBtn.disabled = false;
        if (res.ok) {
          closeDialogEl(dlg);
          pollOnce(true);
        } else if (res.status === 409) {
          showToast("карточку уже изменили, доска обновлена", "error");
          closeDialogEl(dlg);
          pollOnce(true);
        } else {
          errorP.textContent = errText(res);
        }
      });
    });
    foot.appendChild(cancelBtn);
    foot.appendChild(deleteBtn);
    dlg.appendChild(foot);

    dlg.showModal();
  }

  // ---------------------------------------------------------------- новая карточка

  function openNewCardDialog() {
    var dlg = dom.newCardDialog;
    clear(dlg);
    setOpenDialog("new");

    var head = el("div", "dlg-head");
    head.appendChild(el("h2", null, "Новая карточка"));
    head.appendChild(
      closeButton(function () {
        closeDialogEl(dlg);
      })
    );
    dlg.appendChild(head);

    var body = el("div", "dlg-body");
    var form = document.createElement("form");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
    });

    var projectSelectEl = null;
    if (S.project === ALL) {
      projectSelectEl = document.createElement("select");
      for (var i = 0; i < S.data.projects.length; i++) {
        var opt = document.createElement("option");
        opt.value = S.data.projects[i].project;
        opt.textContent = S.data.projects[i].project;
        projectSelectEl.appendChild(opt);
      }
      form.appendChild(formRow("Проект", projectSelectEl));
    }

    var titleInput = document.createElement("input");
    titleInput.type = "text";
    titleInput.required = true;

    var bodyInput = document.createElement("textarea");

    var prioritySelect = document.createElement("select");
    fillOptions(prioritySelect, S.data.meta.priorities, "B", true);

    var tagSelect = document.createElement("select");
    var noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "без метки";
    tagSelect.appendChild(noneOpt);
    fillOptions(tagSelect, S.data.meta.tags, null, false);

    var acceptanceSelect = document.createElement("select");
    fillOptions(acceptanceSelect, S.data.meta.acceptance, "reviewer", false);

    form.appendChild(formRow("Заголовок", titleInput));
    form.appendChild(formRow("Текст", bodyInput));
    form.appendChild(formRow("Приоритет", prioritySelect));
    form.appendChild(formRow("Метка", tagSelect));
    form.appendChild(formRow("Кто принимает", acceptanceSelect));
    var errorP = el("p", "dlg-error");
    form.appendChild(errorP);
    body.appendChild(form);
    dlg.appendChild(body);

    var foot = el("div", "dlg-foot");
    var cancelBtn = mkButton("Отмена", "btn btn-ghost", function () {
      closeDialogEl(dlg);
    });
    var createBtn = mkButton("Создать", "btn btn-primary", function () {
      var title = titleInput.value.trim();
      if (!title) {
        errorP.textContent = "Заголовок обязателен.";
        return;
      }
      var project = projectSelectEl ? projectSelectEl.value : S.project;
      if (!project) {
        errorP.textContent = "Выбери проект.";
        return;
      }
      createBtn.disabled = true;
      request("POST", "/api/cards", {
        project: project,
        title: title,
        body: bodyInput.value,
        priority: prioritySelect.value,
        tag: tagSelect.value || null,
        acceptance: acceptanceSelect.value,
      }).then(function (res) {
        createBtn.disabled = false;
        if (res.ok) {
          closeDialogEl(dlg);
          pollOnce(true);
        } else {
          errorP.textContent = errText(res);
        }
      });
    });
    foot.appendChild(cancelBtn);
    foot.appendChild(createBtn);
    dlg.appendChild(foot);

    dlg.showModal();
  }

  // ---------------------------------------------------------------- запуск

  function cacheDom() {
    dom.connStatus = document.getElementById("conn-status");
    dom.projectSelect = document.getElementById("project-select");
    dom.newCardBtn = document.getElementById("new-card-btn");
    dom.archiveToggle = document.getElementById("archive-toggle");
    dom.themeToggle = document.getElementById("theme-toggle");
    dom.summary = document.getElementById("summary");
    dom.board = document.getElementById("board");
    dom.archiveWrap = document.getElementById("archive-wrap");
    dom.archiveBoard = document.getElementById("archive-board");
    dom.toastRegion = document.getElementById("toast-region");
    dom.cardDialog = document.getElementById("card-dialog");
    dom.menuDialog = document.getElementById("menu-dialog");
    dom.fieldsDialog = document.getElementById("fields-dialog");
    dom.acceptDialog = document.getElementById("accept-dialog");
    dom.newCardDialog = document.getElementById("new-card-dialog");
    dom.editDialog = document.getElementById("edit-dialog");
    dom.confirmDialog = document.getElementById("confirm-dialog");
  }

  function attachStaticHandlers() {
    dom.projectSelect.addEventListener("change", function () {
      S.project = dom.projectSelect.value;
      persistProject(S.project);
      render();
    });
    dom.newCardBtn.addEventListener("click", function () {
      if (S.data) openNewCardDialog();
    });
    dom.archiveToggle.addEventListener("click", function () {
      S.archiveOpen = !S.archiveOpen;
      dom.archiveToggle.setAttribute("aria-pressed", String(S.archiveOpen));
      dom.archiveWrap.hidden = !S.archiveOpen;
    });
    dom.themeToggle.addEventListener("click", function () {
      cycleTheme();
    });

    var dialogs = [
      dom.cardDialog,
      dom.menuDialog,
      dom.fieldsDialog,
      dom.acceptDialog,
      dom.newCardDialog,
      dom.editDialog,
      dom.confirmDialog,
    ];
    for (var i = 0; i < dialogs.length; i++) {
      (function (dlg) {
        dlg.addEventListener("click", function (ev) {
          if (ev.target === dlg) closeDialogEl(dlg);
        });
        // Только для Esc/сворачивания — свои закрытия уже разобраны в closeDialogEl()
        // синхронно; двойная проверка ниже отсекает событие, отложенное браузером до
        // момента, когда счётчик диалогов уже указывает на что-то другое (см. пояснение
        // у closeDialogEl выше).
        dlg.addEventListener("close", function () {
          if (dlg.open) return;
          var kind = kindForDialog(dlg);
          if (S.openDialogKind !== kind) return;
          S.openDialogKind = null;
          if (kind === "card") S.openCard = null;
          flushPendingRenderMaybe();
        });
      })(dialogs[i]);
    }
  }

  function init() {
    cacheDom();
    loadTheme();
    applyTheme();
    attachStaticHandlers();
    document.addEventListener("visibilitychange", onVisibilityChange);
    pollOnce(true);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
