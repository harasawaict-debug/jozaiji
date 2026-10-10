// 年間行事をスプレッドシートから差し替える。
//
// HTMLの #events-year・.events-list・#events-notes は、そのままでも正しく読める
// 「初期表示かつ予備」。取得・検証がすべて通ったときだけ中身を差し替え、
// 失敗したとき（通信エラー・シートの形が変わった・データが空など）は
// 何もしない。画面にエラーは出さない。

(function () {
  var SHEET_ID = '1-qhVZIhpEKTQbd6lU6FfUFPkvy0XVNFLz_C3vz3hbaE';
  var EVENTS_GID = '537295650';
  var NOTES_GID = '393219132';
  var FETCH_TIMEOUT_MS = 8000;
  var EVENTS_HEADER = ['年', '日付', '時刻', '行事名', 'ふりがな', '備考', '掲載'];

  function csvUrl(gid) {
    return 'https://docs.google.com/spreadsheets/d/' + SHEET_ID +
      '/gviz/tq?tqx=out:csv&gid=' + gid;
  }

  // 引用符で囲まれたCSV（カンマ・改行・""による引用符のエスケープ）を読む。
  function parseCsv(text) {
    var rows = [];
    var row = [];
    var field = '';
    var inQuotes = false;

    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (inQuotes) {
        if (c === '"') {
          if (text[i + 1] === '"') {
            field += '"';
            i++;
          } else {
            inQuotes = false;
          }
        } else {
          field += c;
        }
        continue;
      }
      if (c === '"') {
        inQuotes = true;
      } else if (c === ',') {
        row.push(field);
        field = '';
      } else if (c === '\r') {
        // 無視（\r\nの\rも単独の\rもここで吸収する）
      } else if (c === '\n') {
        row.push(field);
        rows.push(row);
        row = [];
        field = '';
      } else {
        field += c;
      }
    }
    if (field.length > 0 || row.length > 0) {
      row.push(field);
      rows.push(row);
    }
    return rows;
  }

  // 取得・解析に失敗したら null を返す（理由は問わない。呼び出し側は
  // nullなら何もせず予備のまま残す）。
  function fetchCsvRows(gid) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, FETCH_TIMEOUT_MS);

    return fetch(csvUrl(gid), { signal: controller.signal })
      .then(function (res) {
        if (!res.ok) {
          return null;
        }
        return res.text().then(parseCsv);
      })
      .catch(function () {
        return null;
      })
      .finally(function () {
        clearTimeout(timer);
      });
  }

  function rowsEqual(a, b) {
    if (a.length !== b.length) {
      return false;
    }
    for (var i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) {
        return false;
      }
    }
    return true;
  }

  function buildEventDtDd(row) {
    var date = row[1];
    var time = row[2];
    var name = row[3];
    var reading = row[4];
    var note = row[5];

    var dt = document.createElement('dt');
    dt.textContent = time && time.trim() ? date + '　' + time : date;

    var dd = document.createElement('dd');
    var nameSpan = document.createElement('span');
    nameSpan.className = 'event-name';
    nameSpan.textContent = name;
    dd.appendChild(nameSpan);

    if (reading && reading.trim()) {
      var readingSpan = document.createElement('span');
      readingSpan.className = 'event-reading';
      readingSpan.textContent = reading;
      dd.appendChild(readingSpan);
    }
    if (note && note.trim()) {
      var noteSpan = document.createElement('span');
      noteSpan.className = 'event-note';
      noteSpan.textContent = note;
      dd.appendChild(noteSpan);
    }

    return [dt, dd];
  }

  function updateEvents() {
    fetchCsvRows(EVENTS_GID).then(function (rows) {
      if (!rows || rows.length < 1) {
        return;
      }
      if (!rowsEqual(rows[0], EVENTS_HEADER)) {
        return;
      }

      var dataRows = rows.slice(1);
      var visible = [];
      for (var i = 0; i < dataRows.length; i++) {
        var row = dataRows[i];
        if (row.length !== EVENTS_HEADER.length) {
          // 列数が合わない行が1つでもあれば、シート全体を信用しない
          return;
        }
        if ((row[6] || '').trim().toUpperCase() === 'TRUE') {
          visible.push(row);
        }
      }
      if (visible.length === 0) {
        return;
      }

      var dl = document.querySelector('#events .events-list');
      if (!dl) {
        return;
      }

      var frag = document.createDocumentFragment();
      for (var j = 0; j < visible.length; j++) {
        var pair = buildEventDtDd(visible[j]);
        frag.appendChild(pair[0]);
        frag.appendChild(pair[1]);
      }
      dl.replaceChildren(frag);
    });
  }

  function updateNotesAndYear() {
    fetchCsvRows(NOTES_GID).then(function (rows) {
      if (!rows) {
        return;
      }

      // 見出し：1行目のB列。空ならHTML直書きの見出しをそのまま残す
      var heading = (rows[0] && typeof rows[0][1] === 'string') ? rows[0][1].trim() : '';
      if (heading) {
        var yearEl = document.getElementById('events-year');
        if (yearEl) {
          yearEl.textContent = heading;
        }
      }

      // 注記：各行のA列。1件も無ければ直書きの注記をそのまま残す
      var notes = [];
      for (var i = 0; i < rows.length; i++) {
        var text = (rows[i][0] || '').trim();
        if (text) {
          notes.push(text);
        }
      }
      if (notes.length > 0) {
        var ul = document.getElementById('events-notes');
        if (ul) {
          var frag = document.createDocumentFragment();
          for (var j = 0; j < notes.length; j++) {
            var li = document.createElement('li');
            li.textContent = notes[j];
            frag.appendChild(li);
          }
          ul.replaceChildren(frag);
        }
      }
    });
  }

  updateEvents();
  updateNotesAndYear();
})();
