# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import copy, re
from anki.utils import intTime, joinFields, splitFields, ids2str,\
    checksum, json
from anki.lang import _
from anki.consts import *
from anki.hooks import runHook
import time

# Models
##########################################################################

# - careful not to add any lists/dicts/etc here, as they aren't deep copied

defaultModel = {
    'sortf': 0,
    'did': 1,
    'latexPre': """\
\\documentclass[12pt]{article}
\\special{papersize=3in,5in}
\\usepackage[utf8]{inputenc}
\\usepackage{amssymb,amsmath}
\\pagestyle{empty}
\\setlength{\\parindent}{0in}
\\begin{document}
""",
    'latexPost': "\\end{document}",
    'mod': 0,
    'usn': 0,
    'vers': [], # FIXME: remove when other clients have caught up
    'type': MODEL_STD,
    'css': """\
.card {
 font-family: arial;
 font-size: 20px;
 text-align: center;
 color: black;
 background-color: white;
}
"""
}

# 字段的默认值，在卡片浏览界面点Fields...按钮时设置。
defaultField = {
    'name': "",
    'ord': None,
    'sticky': False,
    # the following alter editing, and are used as defaults for the
    # template wizard
    'rtl': False,
    'font': "Arial",
    'size': 20,
    # reserved for future use
    'media': [],
}

# 卡片模板的默认值。
#   name: 名字。
#   ord: 索引。
#   qfmt: 问题模板。
#   afmt: 答案模板。
#   did:
#   bqfmt: 卡片浏览时的问题模板。
#   bafmt: 卡片浏览时的答案模板。
defaultTemplate = {
    'name': "",
    'ord': None,
    'qfmt': "",
    'afmt': "",
    'did': None,
    'bqfmt': "",
    'bafmt': "",
    # we don't define these so that we pick up system font size until set
    #'bfont': "Arial",
    #'bsize': 12,
}

class ModelManager:
    """
    笔记类型管理类。

    管理牌组集中所有的笔记类型，每一个笔记类型都用json字串来表示。一个牌组集的所有笔记类型
    全部保存在col表的models字段中。
    """

    # Saving/loading registry
    #############################################################

    def __init__(self, col):
        """
        初始化。

        :param col: 牌组集。
        """
        self.col = col

    def load(self, json_):
        """
        从 json格式的字串中加载数据。

        :param json_: json字符串。
        :return:
        """

        "Load registry from JSON."
        self.changed = False
        self.models = json.loads(json_)

    def save(self, m=None, templates=False):
        """
        标记数据变化，或应用笔记类型m的改动并标记数据变化。

        :param m: 被修改笔记类型，更新该笔记类型中所有的卡片模板与字段之间的依赖关系。
        :param templates: True=检查使用笔记类型m的所有卡片，如果卡片不存在则新增卡片。
        :return: 无。
        """
        "Mark M modified if provided, and schedule registry flush."
        if m and m['id']:
            # 更新修改时间和串号。
            m['mod'] = intTime()
            m['usn'] = self.col.usn()
            self._updateRequired(m)
            if templates:
                self._syncTemplates(m)

        # 内容已经变化。
        self.changed = True
        runHook("newModel")

    def flush(self):
        """如果有任一笔记类型有变化，将变化写回数据库中。"""
        "Flush the registry if any models were changed."
        if self.changed:
            self.col.db.execute("update col set models = ?",
                                 json.dumps(self.models))
            self.changed = False

    # Retrieving and creating models
    #############################################################

    def current(self, forDeck=True):
        """
        返回当前笔记类型。

        :param forDeck: =True 返回当前牌组的笔记类型。
                        否则返回牌组集设置中的当前笔记类型。
        :return: 当前笔记类型
        """
        "Get current model."
        m = self.get(self.col.decks.current().get('mid'))
        if not forDeck or not m:
            m = self.get(self.col.conf['curModel'])
        return m or list(self.models.values())[0]

    def setCurrent(self, m):
        """
        设置当前的笔记类型。

        :param m: 笔记类型
        :return: 无。
        """
        self.col.conf['curModel'] = m['id']
        self.col.setMod()

    def get(self, id):
        """
        根据笔记类型ID获取笔记类型。

        :param id: 笔记类型ID。
        :return: 笔记类型。
        """

        "Get model with ID, or None."
        id = str(id)
        if id in self.models:
            return self.models[id]

    def all(self):
        """返回所有的笔记类型。"""

        "Get all models."
        return list(self.models.values())

    def allNames(self):
        """返回所有的笔记的名字。"""
        return [m['name'] for m in self.all()]

    def byName(self, name):
        """
        通过笔记类型的名字获取笔记类型。

        :param name: 笔记类型的名字
        :return: 笔记类型。
        """

        "Get model with NAME."
        for m in list(self.models.values()):
            if m['name'] == name:
                return m

    def new(self, name):
        """
        新建笔记类型。
        :param name: 笔记类型的名字。
        :return: 笔记类型。
        """

        "Create a new model, save it in the registry, and return it."
        # caller should call save() after modifying
        m = defaultModel.copy()
        m['name'] = name
        m['mod'] = intTime()
        m['flds'] = []
        m['tmpls'] = []
        m['tags'] = []
        m['id'] = None
        return m

    def rem(self, m):
        """
        删除笔记类型和使用这个笔记类型的所有卡片和笔记。

        :param m: 笔记类型。
        :return: 无。
        """

        "Delete model, and all its cards/notes."
        self.col.modSchema(check=True)

        # 要删除的笔记类型是否为当前笔记类型。
        current = self.current()['id'] == m['id']

        # 删除使用这个笔记类型的所有卡片和笔记。
        # delete notes/cards
        self.col.remCards(self.col.db.list("""
select id from cards where nid in (select id from notes where mid = ?)""",
                                      m['id']))
        # 删除这个笔记类型。
        # then the model
        del self.models[str(m['id'])]

        # 标记数据变动。
        self.save()

        # GUI应该保证最后的笔记类型不能被删除。
        # GUI should ensure last model is not deleted
        if current:
            # 更新当前笔记类型为默认笔记类型。
            self.setCurrent(list(self.models.values())[0])

    def add(self, m):
        """
        加入一条笔记类型。

        :param m: 新加入的笔记类型。
        :return: 无。
        """

        # 设置笔记ID。
        self._setID(m)

        # 加入新的笔记类型m。
        self.update(m)

        # 设置当前的笔记类型为m
        self.setCurrent(m)

        # 标记改变。
        self.save(m)

    def ensureNameUnique(self, m):
        """
        确保笔记类型m与已有的笔记类型名字不重复。

        如果有重复，将笔记类型m的名字后面加一个长度为5的随机的字串。
        :param m: 笔记类型
        :return: 无。
        """
        for mcur in self.all():
            if (mcur['name'] == m['name'] and
                mcur['id'] != m['id']):
                    m['name'] += "-" + checksum(str(time.time()))[:5]
                    break

    def update(self, m):
        """添加或更新笔记。"""
        "Add or update an existing model. Used for syncing and merging."
        self.ensureNameUnique(m)
        self.models[str(m['id'])] = m
        # mark registry changed, but don't bump mod time
        self.save()

    def _setID(self, m):
        """
        为笔记类型设置一个不重复的ID。

        :param m: 笔记类型。
        :return: 无。
        """

        while 1:
            id = str(intTime(1000))
            if id not in self.models:
                break
        m['id'] = id

    def have(self, id):
        """笔记类型id是否存在。"""
        return str(id) in self.models

    def ids(self):
        """返回所有笔记类型的ID列表。"""
        return list(self.models.keys())

    # Tools
    ##################################################

    def nids(self, m):
        """返回使用笔记类型m的所有笔记ID列表。"""
        "Note ids for M."
        return self.col.db.list(
            "select id from notes where mid = ?", m['id'])

    def useCount(self, m):
        """返回使用笔记类型m的笔记总数。"""
        "Number of note using M."
        return self.col.db.scalar(
            "select count() from notes where mid = ?", m['id'])

    def tmplUseCount(self, m, ord):
        """返回使用笔记类型m中的卡片模板ord的卡片总数。"""
        return self.col.db.scalar("""
select count() from cards, notes where cards.nid = notes.id
and notes.mid = ? and cards.ord = ?""", m['id'], ord)

    # Copying
    ##################################################

    def copy(self, m):
        """添加卡片类型m的一个复本。"""
        "Copy, save and return."
        m2 = copy.deepcopy(m)
        m2['name'] = _("%s copy") % m2['name']
        self.add(m2)
        return m2

    # Fields
    ##################################################

    def newField(self, name):
        """返回一个名字为name的新字段。"""
        f = defaultField.copy()
        f['name'] = name
        return f

    def fieldMap(self, m):
        """返回图结构的字段数据。字段名为key，内容是这个字段的索引和数据。"""
        "Mapping of field name -> (ord, field)."
        return dict((f['name'], (f['ord'], f)) for f in m['flds'])

    def fieldNames(self, m):
        """返回笔记类型中所有字段的名字列表。"""
        return [f['name'] for f in m['flds']]

    def sortIdx(self, m):
        """返回笔记类型m的排序字段。"""
        return m['sortf']

    def setSortIdx(self, m, idx):
        """设置笔记型m的排序字段为idx。"""
        assert 0 <= idx < len(m['flds'])
        self.col.modSchema(check=True)

        # 更新笔记的排序字段。
        m['sortf'] = idx
        self.col.updateFieldCache(self.nids(m))
        self.save(m)

    def addField(self, m, field):
        """为笔记类型m添加字段field。"""
        # only mod schema if model isn't new
        if m['id']:
            self.col.modSchema(check=True)

        # 笔记类型增加一个空字段。
        m['flds'].append(field)

        # 更新笔记的排序字段。
        self._updateFieldOrds(m)
        self.save(m)

        # 笔记的添加一个内容为空的字段。
        def add(fields):
            fields.append("")
            return fields
        self._transformFields(m, add)

    def remField(self, m, field):
        """笔记类型m删除字段field"""
        self.col.modSchema(check=True)

        # 获取排序字段的名字，用于后面找回排序字段。
        # save old sort field
        sortFldName = m['flds'][m['sortf']]['name']

        # 获取要删除的字段引用，用于后面删除笔记中的相应字段。
        idx = m['flds'].index(field)

        # 删除笔记类型中的字段。
        m['flds'].remove(field)

        # 找回排序字段的索引，找不到就默认排序字段的索引为0。
        # restore old sort field if possible, or revert to first field
        m['sortf'] = 0
        for c, f in enumerate(m['flds']):
            if f['name'] == sortFldName:
                m['sortf'] = c
                break

        # 更新字段的索引。
        self._updateFieldOrds(m)

        # 删除笔记中的对应字段。
        def delete(fields):
            del fields[idx]
            return fields
        self._transformFields(m, delete)

        if m['flds'][m['sortf']]['name'] != sortFldName:
            # 如果排序字段发生变化那么更新笔记的排序字段。
            # need to rebuild sort field
            self.col.updateFieldCache(self.nids(m))

        # 删除卡片模板中出现的这个字段的名字并保存和应用笔记类型的修改。
        # saves
        self.renameField(m, field, None)

    def moveField(self, m, field, idx):
        """
        修改字段的顺序。

        :param m: 要修改的笔记类型。
        :param field: 要修改顺序的字段。
        :param idx: 新的索经。
        :return: 无。
        """

        self.col.modSchema(check=True)

        # 位置没有变化，不做修改。
        oldidx = m['flds'].index(field)
        if oldidx == idx:
            return

        # 记忆变动前的排序字段。
        # remember old sort field
        sortf = m['flds'][m['sortf']]

        # 移动位置，先删除后插入。
        # move
        m['flds'].remove(field)
        m['flds'].insert(idx, field)

        # 更新排序字段的索引。
        # restore sort field
        m['sortf'] = m['flds'].index(sortf)

        # 更新所有字段的索引。
        self._updateFieldOrds(m)

        # 保存并应用修改。
        self.save(m)

        # 修改笔记中保存的字段数据中的顺序。
        def move(fields, oldidx=oldidx):
            val = fields[oldidx]
            del fields[oldidx]
            fields.insert(idx, val)
            return fields
        self._transformFields(m, move)

    def renameField(self, m, field, newName):
        """重命名字段，包括更改卡片模板中出现的这个字段的名字。"""
        self.col.modSchema(check=True)
        pat = r'{{([^{}]*)([:#^/]|[^:#/^}][^:}]*?:|)%s}}'
        def wrap(txt):
            def repl(match):
                return '{{' + match.group(1) + match.group(2) + txt +  '}}'
            return repl
        for t in m['tmpls']:
            for fmt in ('qfmt', 'afmt'):
                if newName:
                    t[fmt] = re.sub(
                        pat % re.escape(field['name']), wrap(newName), t[fmt])
                else:
                    t[fmt] = re.sub(
                        pat  % re.escape(field['name']), "", t[fmt])
        field['name'] = newName
        self.save(m)

    def _updateFieldOrds(self, m):
        """更新字段的索引。"""
        for c, f in enumerate(m['flds']):
            f['ord'] = c

    def _transformFields(self, m, fn):
        """
        为使用笔记类型m的所有笔记转换字段内容。

        :param m: 笔记类型。
        :param fn: 转换字段内容的回调函数。
        :return: 无。
        """
        # model hasn't been added yet?
        if not m['id']:
            return
        r = []

        # 遍历笔记类型m的所有笔记，并调用转换回调函数
        # 更新字段内容。
        for (id, flds) in self.col.db.execute(
            "select id, flds from notes where mid = ?", m['id']):
            r.append((joinFields(fn(splitFields(flds))),
                      intTime(), self.col.usn(), id))

        # 批量更新。
        self.col.db.executemany(
            "update notes set flds=?,mod=?,usn=? where id = ?", r)

    # Templates
    ##################################################

    def newTemplate(self, name):
        """
        返回新卡片模板。

        :param name: 新卡片模板的名字。
        :return: 新卡片模板。
        """
        t = defaultTemplate.copy()
        t['name'] = name
        return t

    def addTemplate(self, m, template):
        """
        新增卡片模板。

        注意：之后应该调用col.genCards()来产生使用这个新增卡片模板的卡片。

        :param m: 要新增加卡片模板的笔记类型。
        :param template: 要新增加的卡片模板。
        :return: 无。
        """
        "Note: should col.genCards() afterwards."
        if m['id']:
            self.col.modSchema(check=True)
        m['tmpls'].append(template)
        self._updateTemplOrds(m)
        self.save(m)

    def remTemplate(self, m, template):
        """
        删除模板。

        :param m: 笔记类型。
        :param template: 要删除的卡片模板。
        :return: 如果有孤立的笔记，则返回False。
        """

        "False if removing template would leave orphan notes."
        assert len(m['tmpls']) > 1

        # 找出所有使用这个卡片模板的卡片。
        # find cards using this template
        ord = m['tmpls'].index(template)
        cids = self.col.db.list("""
select c.id from cards c, notes f where c.nid=f.id and mid = ? and ord = ?""",
                                 m['id'], ord)
        # all notes with this template must have at least two cards, or we
        # could end up creating orphaned notes
        if self.col.db.scalar("""
select nid, count() from cards where
nid in (select nid from cards where id in %s)
group by nid
having count() < 2
limit 1""" % ids2str(cids)):
            return False
        # ok to proceed; remove cards
        self.col.modSchema(check=True)
        self.col.remCards(cids)
        # shift ordinals
        self.col.db.execute("""
update cards set ord = ord - 1, usn = ?, mod = ?
 where nid in (select id from notes where mid = ?) and ord > ?""",
                             self.col.usn(), intTime(), m['id'], ord)
        m['tmpls'].remove(template)
        self._updateTemplOrds(m)
        self.save(m)
        return True

    def _updateTemplOrds(self, m):
        for c, t in enumerate(m['tmpls']):
            t['ord'] = c

    def moveTemplate(self, m, template, idx):
        oldidx = m['tmpls'].index(template)
        if oldidx == idx:
            return
        oldidxs = dict((id(t), t['ord']) for t in m['tmpls'])
        m['tmpls'].remove(template)
        m['tmpls'].insert(idx, template)
        self._updateTemplOrds(m)
        # generate change map
        map = []
        for t in m['tmpls']:
            map.append("when ord = %d then %d" % (oldidxs[id(t)], t['ord']))
        # apply
        self.save(m)
        self.col.db.execute("""
update cards set ord = (case %s end),usn=?,mod=? where nid in (
select id from notes where mid = ?)""" % " ".join(map),
                             self.col.usn(), intTime(), m['id'])

    def _syncTemplates(self, m):
        """为使用笔记类型m的所有笔记重新根据卡片模板生成卡片。"""
        rem = self.col.genCards(self.nids(m))

    # Model changing
    ##########################################################################
    # - maps are ord->ord, and there should not be duplicate targets
    # - newModel should be self if model is not changing

    def change(self, m, nids, newModel, fmap, cmap):
        self.col.modSchema(check=True)
        assert newModel['id'] == m['id'] or (fmap and cmap)
        if fmap:
            self._changeNotes(nids, newModel, fmap)
        if cmap:
            self._changeCards(nids, m, newModel, cmap)
        self.col.genCards(nids)

    def _changeNotes(self, nids, newModel, map):
        d = []
        nfields = len(newModel['flds'])
        for (nid, flds) in self.col.db.execute(
            "select id, flds from notes where id in "+ids2str(nids)):
            newflds = {}
            flds = splitFields(flds)
            for old, new in list(map.items()):
                newflds[new] = flds[old]
            flds = []
            for c in range(nfields):
                flds.append(newflds.get(c, ""))
            flds = joinFields(flds)
            d.append(dict(nid=nid, flds=flds, mid=newModel['id'],
                      m=intTime(),u=self.col.usn()))
        self.col.db.executemany(
            "update notes set flds=:flds,mid=:mid,mod=:m,usn=:u where id = :nid", d)
        self.col.updateFieldCache(nids)

    def _changeCards(self, nids, oldModel, newModel, map):
        d = []
        deleted = []
        for (cid, ord) in self.col.db.execute(
            "select id, ord from cards where nid in "+ids2str(nids)):
            # if the src model is a cloze, we ignore the map, as the gui
            # doesn't currently support mapping them
            if oldModel['type'] == MODEL_CLOZE:
                new = ord
                if newModel['type'] != MODEL_CLOZE:
                    # if we're mapping to a regular note, we need to check if
                    # the destination ord is valid
                    if len(newModel['tmpls']) <= ord:
                        new = None
            else:
                # mapping from a regular note, so the map should be valid
                new = map[ord]
            if new is not None:
                d.append(dict(
                    cid=cid,new=new,u=self.col.usn(),m=intTime()))
            else:
                deleted.append(cid)
        self.col.db.executemany(
            "update cards set ord=:new,usn=:u,mod=:m where id=:cid",
            d)
        self.col.remCards(deleted)

    # Schema hash
    ##########################################################################

    def scmhash(self, m):
        "Return a hash of the schema, to see if models are compatible."
        s = ""
        for f in m['flds']:
            s += f['name']
        for t in m['tmpls']:
            s += t['name']
        return checksum(s)

    # Required field/text cache
    ##########################################################################

    def _updateRequired(self, m):
        """
            更新笔记类型中所有的卡片模板与字段之间的依赖关系

            依赖关系保存在m['req']中，格式为(卡片模板索引，依赖方式， 相关字段列表)。
            依赖方式有：
                none: 此卡片模板与所有字段无任何关系。
                all: 相关字段必须全部不为空时，此卡片模板才有效。
                any: 相关字段只要有一个不为空时，此卡片模板就有效。
        """

        # 填空型的笔记类型不需要依赖关系。
        if m['type'] == MODEL_CLOZE:
            # nothing to do
            return

        # 遍历笔记类型中的所有卡片模板。
        req = []
        flds = [f['name'] for f in m['flds']]
        for t in m['tmpls']:
            # 获得这个卡片模板和字段之间的依赖关系。
            ret = self._reqForTemplate(m, flds, t)
            req.append((t['ord'], ret[0], ret[1]))
        m['req'] = req

    def _reqForTemplate(self, m, flds, t):
        """
        当使用笔记类型m的卡片模板t时，卡片的问题与每个字段的依赖关系。

        :param m: 笔记类型。
        :param flds: 笔记类型中定义的所有字段名。
        :param t: 笔记类型中的某个卡片模板。
        :return:
                    1. 所有字段都不影响卡片的问题时，返回："none", [], []；？多了一个[]
                    2. 有必填字段时返回：'all', [必填字段的索引列表]；
                    3. 至少要有一个这样的字段时返回：'any', [任选字段的索引列表]。
        """

        # 测试全部字段不为空时的问题full和全部字段都为空时的问题empty。
        # 如果两种情况下的问题完全相同，说明模板完全没有依赖的字段，
        # 这里判定卡片模板无效。返回 "none", [], []。
        a = []
        b = []
        for f in flds:
            a.append("ankiflag")
            b.append("")
        data = [1, 1, m['id'], 1, t['ord'], "", joinFields(a)]
        full = self.col._renderQA(data)['q']
        data = [1, 1, m['id'], 1, t['ord'], "", joinFields(b)]
        empty = self.col._renderQA(data)['q']
        # if full and empty are the same, the template is invalid and there is
        # no way to satisfy it
        if full == empty:
            return "none", [], []

        # 测试每一个字段：当这个字段为空，而其它字段不为空时，
        # 如果所有字段的内容都不出现在问题中时，说明这个字段的必填的。
        # 当这样的字段存在时，返回 'all', [必填字段的索引]。
        type = 'all'
        req = []
        for i in range(len(flds)):
            tmp = a[:]
            tmp[i] = ""
            data[6] = joinFields(tmp)
            # if no field content appeared, field is required
            if "ankiflag" not in self.col._renderQA(data)['q']:
                req.append(i)
        if req:
            return type, req

        # 测试每一个字段：当这个字段不为空，而其它字段都为空时，
        # 如果问题不为‘空’，说明这个字段可以不让问题为‘空’。
        # 说明至少要有一个这样的字段才能让问题不为‘空’。
        # 当这样的字段存在时，返回 'any', [任选字段的索引]。
        # if there are no required fields, switch to any mode
        type = 'any'
        req = []
        for i in range(len(flds)):
            tmp = b[:]
            tmp[i] = "1"
            data[6] = joinFields(tmp)
            # if not the same as empty, this field can make the card non-blank
            if self.col._renderQA(data)['q'] != empty:
                req.append(i)
        return type, req

    def availOrds(self, m, flds):
        """
        根据字段数据，找出笔记类型中所有可用的卡片模板。

        也就是用这些字段数据后，可以正常生成卡片的卡片模板。

        :param m: 笔记类型。
        :param flds: 字段数据。
        :return: 可用的卡片模板。
        """

        "Given a joined field string, return available template ordinals."
        if m['type'] == MODEL_CLOZE:
            # 返回填空型的可用卡片模板索引。
            return self._availClozeOrds(m, flds)

        # 下面是针对标准型的笔记类型。

        # 获取所有字段内容到fields列表中。
        fields = {}
        for c, f in enumerate(splitFields(flds)):
            fields[c] = f.strip()

        # 根据之前通过_updateRequired生成的字段与卡片模板之间的依赖关系，
        # 遍历所有依赖关系，找出可用的模板放到avail。
        avail = []
        for ord, type, req in m['req']:
            # unsatisfiable template
            if type == "none":
                # 这个卡片模板不可用，下一个。
                continue
            # AND requirement?
            elif type == "all":
                # 所有必填字段不为空时，这个卡片模板可用。
                ok = True
                for idx in req:
                    if not fields[idx]:
                        # missing and was required
                        ok = False
                        break
                if not ok:
                    continue
            # OR requirement?
            elif type == "any":
                # 任一可选字段不为空时，这个卡片模板可用。
                ok = False
                for idx in req:
                    if fields[idx]:
                        ok = True
                        break
                if not ok:
                    continue
            avail.append(ord)
        return avail

    def _availClozeOrds(self, m, flds, allowEmpty=True):
        """
        针对填空型的笔记类型，给出字段内容， 找出可用的卡片模板。
        :param m: 填空型的笔记类型。
        :param flds: 字段内容。
        :param allowEmpty: 是否充许
        :return: 返回可用的卡片模板列表。
        """
        sflds = splitFields(flds)
        map = self.fieldMap(m)
        ords = set()

        # 从卡片模板的问题模板查找{{cloze:字段名}}和<%cloze:字段名%>出现的位置。
        # 然后分析哪些字段用到了填空，即哪些字段是需要识别类似{{c1::1913}}这样的填空。
        # 然后分析这些字段用到了哪些 {{c数字::答案}}，将这些数字记录在oders集合中。
        matches = re.findall("{{[^}]*?cloze:(?:[^}]?:)*(.+?)}}", m['tmpls'][0]['qfmt'])
        matches += re.findall("<%cloze:(.+?)%>", m['tmpls'][0]['qfmt'])
        for fname in matches:
            if fname not in map:
                continue
            ord = map[fname][0]
            # 找出c1，c2,...中所有的数字，追加到ords集合中。
            ords.update([int(m)-1 for m in re.findall(
                "(?s){{c(\d+)::.+?}}", sflds[ord])])
        # 去掉不合法的数字。
        if -1 in ords:
            ords.remove(-1)
        if not ords and allowEmpty:
            # empty clozes use first ord
            return [0]

        # ords列表中有几个数字，就有几个卡片。
        # 例如当ords=(0,1,3),表示所有有填空的字段中一共出现过c1，c2和c4。
        # 这样就要有三张卡片分别对c1，c2，c4填空进行提问，相当于有三个模板，
        # 虽然实际上只有一个卡片模板。
        return list(ords)

    # Sync handling
    ##########################################################################

    def beforeUpload(self):
        for m in self.all():
            m['usn'] = 0
        self.save()
