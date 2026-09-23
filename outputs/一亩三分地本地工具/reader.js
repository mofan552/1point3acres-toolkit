'use strict';
const data = JSON.parse(document.getElementById('dataset').textContent);
const ui = data.ui, copy = ui.copy;
let filtered = data.records, selected = null;
const element = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const byId = id => document.getElementById(id);
const link = (text, url, cls) => {
  const node = element('a', text, cls);
  node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer';
  return node;
};
const title = data.company + ' ' + copy.title_suffix;
document.title = title;
byId('title').textContent = title;
byId('kicker').textContent = copy.kicker;
byId('subtitle').textContent = copy.subtitle;
byId('query').placeholder = copy.search_placeholder;
byId('query').setAttribute('aria-label', copy.search_label);
byId('status').setAttribute('aria-label', copy.filter_label);
for (const filter of ui.filters) {
  const option = element('option', filter.label); option.value = filter.value; byId('status').append(option);
}
for (const [id, values, label] of [['role', ui.facets.roles, copy.role_label], ['level', ui.facets.levels, copy.level_label]]) {
  const select = byId(id); select.setAttribute('aria-label', label);
  const any = element('option', label + ': ' + copy.any); any.value = ''; select.append(any);
  for (const value of values) { const option = element('option', value); option.value = value; select.append(option); }
}
byId('date_from').setAttribute('aria-label', copy.date_from_label); byId('date_from').title = copy.date_from_label;
byId('date_to').setAttribute('aria-label', copy.date_to_label); byId('date_to').title = copy.date_to_label;
byId('reset').textContent = copy.reset;
for (const stat of ui.stats) {
  const box = element('div'); box.append(element('strong', String(stat.value)), element('span', stat.label)); byId('stats').append(box);
}
byId('foot').textContent = copy.export_time + ': ' + new Date(data.exported_at).toLocaleString() + ' · ' + data.notes;

function toolbar() {
  const area = element('div', undefined, 'toolbar');
  for (const item of ui.downloads) {
    const a = element('a', item.label); a.href = ui.files[item.key]; a.download = ui.files[item.key]; area.append(a);
  }
  const download = element('button', copy.export_search);
  download.onclick = () => {
    const records = filtered.map(({display, ...record}) => record);
    const {ui: ignored, ...payload} = data;
    const url = URL.createObjectURL(new Blob([JSON.stringify({...payload, records}, null, 2)], {type: 'application/json'}));
    const a = element('a'); a.href = url; a.download = data.company + copy.search_file; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  area.append(download); return area;
}

function show(record) {
  selected = record.tid;
  const area = byId('detail'); area.replaceChildren();
  area.append(element('h2', record.title), element('div', [record.company, record.role, record.level, record.listed_date || copy.unknown_date].join(' · '), 'meta'));
  const badges = element('div'); for (const tag of record.tags) badges.append(element('span', tag, 'badge')); area.append(badges);
  area.append(element('div', record.display.label + ' · ' + copy.saved + ' ' + record.posts.length + ' ' + copy.entries + ' / ' + copy.expected + ' ' + (record.expected_posts ?? copy.unknown), 'meta'));
  if (record.display.needs_notice) area.append(element('div', copy.missing, 'notice'));
  area.append(link(copy.source, record.url, 'source'), toolbar());
  record.posts.forEach((post, index) => {
    area.append(element('h3', index === 0 ? copy.main_post : copy.reply + ' ' + index), element('div', post.text, 'body'));
    if (post.authored_at) area.append(element('div', post.authored_at, 'meta'));
    if (post.images?.length) {
      for (const image of post.images) {
        if (image.local) {
          const figure = element('figure', undefined, 'figure');
          const img = element('img'); img.src = image.local; img.alt = image.alt || ''; img.loading = 'lazy';
          figure.append(img, link(copy.image_source, image.url, 'source'));
          if (image.recognized?.text) {
            figure.append(element('div', copy.recognized_text, 'meta'), element('pre', image.recognized.text, 'recognized'));
          }
          area.append(figure);
        } else {
          area.append(element('div', copy.image_note, 'meta'), link(image.alt || copy.image_source, image.url, 'source'), element('br'));
        }
      }
    }
    if (post.attachments?.length) {
      for (const item of post.attachments) {
        if (item.local) area.append(link(item.name || copy.attachment, item.local, 'source'), element('br'));
        else area.append(element('div', (item.name || copy.attachment) + ' · ' + (item.restricted ? copy.attachment_restricted : copy.attachment_missing), 'meta'));
      }
    }
  });
  drawCards();
}

function drawCards() {
  const cards = byId('cards'); cards.replaceChildren();
  for (const record of filtered) {
    const button = element('button', undefined, 'card' + (record.tid === selected ? ' active' : ''));
    button.append(element('b', record.title), element('small', (record.listed_date || copy.unknown_date) + ' · ' + record.posts.length + ' ' + copy.entries));
    button.append(element('span', record.display.short, 'badge ' + record.display.tone));
    for (const tag of record.tags.slice(0, 3)) button.append(element('span', tag, 'badge'));
    button.onclick = () => show(record); cards.append(button);
  }
  if (!filtered.length) cards.append(element('div', copy.empty_list, 'empty'));
  byId('count').textContent = copy.found + ' ' + filtered.length + ' / ' + data.records.length + ' ' + copy.threads;
}

function filter() {
  const query = byId('query').value.trim().toLowerCase(), status = byId('status').value;
  const role = byId('role').value, level = byId('level').value, since = byId('date_from').value, until = byId('date_to').value;
  filtered = data.records.filter(record => {
    const facets = record.display.facets;
    if (!record.display.filters.includes(status) || !facets.search_text.includes(query)) return false;
    if (role && facets.role !== role) return false;
    if (level && facets.level !== level) return false;
    if ((since || until) && !facets.post_date) return false;
    if (since && facets.post_date < since) return false;
    if (until && facets.post_date > until) return false;
    return true;
  });
  drawCards();
  if (filtered.length && !filtered.some(record => record.tid === selected)) show(filtered[0]);
  if (!filtered.length) { selected = null; byId('detail').replaceChildren(element('div', copy.empty_detail, 'empty')); }
}
byId('query').addEventListener('input', filter);
for (const id of ['status', 'role', 'level', 'date_from', 'date_to']) byId(id).addEventListener('change', filter);
byId('reset').addEventListener('click', () => {
  byId('query').value = ''; byId('status').value = 'all';
  for (const id of ['role', 'level', 'date_from', 'date_to']) byId(id).value = '';
  filter();
});
if (data.records.length) show(data.records[0]);
else byId('detail').append(element('div', copy.empty_library, 'empty'));
drawCards();
