"""One catalog for reader copy and a projection of shared business states."""
import json
from contracts import CollectionStatus, ContentStatus, record_state, record_post_date, record_search_text, UNLABELLED
from settings import ROOT, EXPORT_FILES

COPY = {
    'kicker': 'INTERVIEW NOTEBOOK', 'title_suffix': '面经资料',
    'subtitle': '按题型检索，结合原文和回复复习。每篇都保留来源与内容完整性。',
    'search_label': '搜索面经', 'search_placeholder': '搜索题目、关键词或代码片段',
    'filter_label': '内容完整性', 'empty_list': '没有找到匹配内容。试试更短的关键词。',
    'empty_detail': '当前筛选没有匹配的面经。', 'empty_library': '资料库尚无内容。',
    'missing': '当前记录包含未取得的内容。正文中的缺失位置已标记；请结合原帖判断，不能视为完整题目。',
    'source': '打开原帖', 'main_post': '主楼', 'reply': '回复', 'unknown_date': '日期未标注',
    'image_note': '本条包含图片，尚未归档（离线不可见）：', 'image_source': '查看图片来源',
    'attachment': '附件', 'attachment_restricted': '权限受限，未下载', 'attachment_missing': '未归档，仅有来源',
    'recognized_text': '图片识别文本（机器识别，非原文，可能有误）', 'matched_in_recognized': '命中于图片识别文本',
    'export_time': '导出时间', 'export_search': '导出搜索结果', 'search_file': '搜索结果.json',
    'found': '找到', 'threads': '篇', 'entries': '条内容', 'saved': '已保存', 'expected': '预期',
    'unknown': '未知',
    'role_label': '岗位', 'level_label': '级别', 'date_from_label': '发帖日期起', 'date_to_label': '发帖日期止',
    'any': '不限', 'reset': '重置筛选',
}
DISPLAY = {
    CollectionStatus.COMPLETE: {'label': '正文与分页完整', 'short': '完整', 'tone': 'complete'},
    CollectionStatus.RESTRICTED: {'label': '正文部分受限', 'short': '部分受限', 'tone': 'limited'},
    CollectionStatus.PARTIAL_PAGES: {'label': '回复尚未采完', 'short': '分页未完', 'tone': 'limited'},
    CollectionStatus.VISIBLE: {'label': '完整性待核实', 'short': '待核实', 'tone': 'limited'},
}


def reader_payload(payload):
    records = []
    for record in payload['records']:
        state = record_state(record)
        filters = ['all']
        if record['complete']:
            filters.append(CollectionStatus.COMPLETE)
        if record['content_status'] == ContentStatus.RESTRICTED:
            filters.append(ContentStatus.RESTRICTED)
        if not record['pagination_complete']:
            filters.append(CollectionStatus.PARTIAL_PAGES)
        # Facets are precomputed here with the same contract functions the library search uses, so the
        # reader only compares values and never re-derives what "posting date" or "search text" mean.
        facets = {'role': record.get('role') or UNLABELLED, 'level': record.get('level') or UNLABELLED,
                  'post_date': record_post_date(record), 'search_text': record_search_text(record).lower()}
        records.append({**record, 'display': {**DISPLAY[state], 'state': state, 'filters': filters,
                                           'needs_notice': state != CollectionStatus.COMPLETE, 'facets': facets}})
    return {**payload, 'records': records, 'ui': {'copy': COPY, 'files': EXPORT_FILES,
        'facets': {'roles': sorted({r['display']['facets']['role'] for r in records}),
                   'levels': sorted({r['display']['facets']['level'] for r in records})},
        'filters': [{'value': key, 'label': label} for key, label in
                    [('all', '全部内容'), (CollectionStatus.COMPLETE, '正文与分页完整'),
                     (ContentStatus.RESTRICTED, '含受限正文'), (CollectionStatus.PARTIAL_PAGES, '回复尚未采完')]],
        'downloads': [{'key': key, 'label': label} for key, label in
                      [('json', '导出 JSON'), ('csv', '导出 CSV'), ('markdown', '阅读 Markdown')]],
        'stats': [{'value': value, 'label': label} for value, label in
                 [(len(records), '篇面经'), (sum(r['complete'] for r in records), '篇正文与分页完整'),
                  (sum(len(r['posts']) for r in records), '条主楼与回复')]]}}


def render_reader(payload):
    embedded = json.dumps(reader_payload(payload), ensure_ascii=False).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    css = '\n'.join((ROOT / name).read_text(encoding='utf-8') for name in ['tokens.css', 'reader.css'])
    script = (ROOT / 'reader.js').read_text(encoding='utf-8')
    template = (ROOT / 'reader.html').read_text(encoding='utf-8')
    return template.replace('__STYLE__', css).replace('__SCRIPT__', script).replace('__DATA__', embedded)
