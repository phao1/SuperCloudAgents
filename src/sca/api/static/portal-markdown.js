'use strict';

// All report content enters the document as text or validated link destinations.
const portalMarkdown = (() => {
  const heading = /^ {0,3}(#{1,6})\s+(.+)$/u;
  const fence = /^ {0,3}(\x60{3,}|~{3,})[^~\x60]*$/u;
  const item = /^ {0,3}([-+*]|\d{1,9}[.)])\s+(\S.*)$/u;

  function inline(node, text) {
    const tokens = /\x60([^\x60\n]+)\x60|(!?)\[([^[\]\n]+)\]\(([^()\s]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_/gu;
    let end = 0;
    for (const match of text.matchAll(tokens)) {
      const [raw, code, image, label, uri, bold, boldAlt, italic, italicAlt] = match;
      node.append(document.createTextNode(text.slice(end, match.index)));
      if (code !== undefined) node.append(el('code', code));
      else if (label !== undefined) {
        node.append(image ? document.createTextNode(raw) : portalUI.externalLink(label, uri, true));
      } else if (bold !== undefined || boldAlt !== undefined) node.append(el('strong', bold ?? boldAlt));
      else node.append(el('em', italic ?? italicAlt));
      end = match.index + raw.length;
    }
    node.append(document.createTextNode(text.slice(end)));
  }

  function cells(line) {
    return line.trim().replace(/^\||(?<!\\)\|$/gu, '')
      .split(/(?<!\\)\|/u).map(cell => cell.trim().replace(/\\\|/gu, '|'));
  }

  function tableHeader(lines, i) {
    if (!lines[i]?.includes('|') || !lines[i + 1]?.includes('-')) return null;
    const headers = cells(lines[i]);
    const dividers = cells(lines[i + 1]);
    return headers.length === dividers.length && dividers.every(v => /^:?-{3,}:?$/u.test(v))
      ? headers : null;
  }

  function appendRow(section, tag, columns, width) {
    const row = el('tr');
    for (let j = 0; j < width; j++) {
      const cell = el(tag);
      if (tag === 'th') cell.scope = 'col';
      inline(cell, columns[j] || '');
      row.append(cell);
    }
    section.append(row);
  }

  function render(text) {
    const fragment = document.createDocumentFragment();
    const lines = String(text).split(/\r\n?|\n/u);
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i++;
        continue;
      }
      const fenced = line.match(fence);
      const title = line.match(heading);
      const firstItem = line.match(item);
      const headers = tableHeader(lines, i);
      if (fenced) {
        const marker = fenced[1];
        const closing = new RegExp('^ {0,3}' + marker[0] + '{' + marker.length + ',}\\s*$');
        const code = [];
        i++;
        while (i < lines.length && !closing.test(lines[i])) code.push(lines[i++]);
        if (i < lines.length) i++;
        const pre = el('pre');
        pre.append(el('code', code.join('\n')));
        fragment.append(pre);
      } else if (title) {
        const tag = el('h' + title[1].length);
        inline(tag, title[2].replace(/\s+#+\s*$/u, ''));
        fragment.append(tag);
        i++;
      } else if (firstItem) {
        const ordered = /^\d/u.test(firstItem[1]);
        const list = el(ordered ? 'ol' : 'ul');
        if (ordered) list.start = Number.parseInt(firstItem[1], 10);
        while (i < lines.length) {
          const next = lines[i].match(item);
          if (!next || /^\d/u.test(next[1]) !== ordered) break;
          const entry = el('li');
          inline(entry, next[2]);
          list.append(entry);
          i++;
        }
        fragment.append(list);
      } else if (headers) {
        const wrap = portalUI.scroll('报告表格'), grid = el('table');
        const head = el('thead'), body = el('tbody');
        appendRow(head, 'th', headers, headers.length);
        i += 2;
        while (i < lines.length && lines[i].includes('|') && !heading.test(lines[i])) {
          appendRow(body, 'td', cells(lines[i++]), headers.length);
        }
        grid.append(head, body);
        wrap.append(grid);
        fragment.append(wrap);
      } else {
        const paragraph = [line];
        i++;
        while (i < lines.length && lines[i].trim() && !heading.test(lines[i])
          && !fence.test(lines[i]) && !item.test(lines[i]) && !tableHeader(lines, i)) {
          paragraph.push(lines[i++]);
        }
        const p = el('p');
        inline(p, paragraph.join(' '));
        fragment.append(p);
      }
    }
    return fragment;
  }

  return {render};
})();
