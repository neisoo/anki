# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.utils import fieldChecksum, intTime, \
    joinFields, splitFields, stripHTMLMedia, timestampID, guid64

# guid：笔记的唯一ID。
# scm: ?
# mod: 修改时间
# mid: 笔记类型ID
# usn: 牌组集ID
# tags: 标签
# fields: 字段
# sfld: 根据排序字段生成的字符串，用于笔记排序。
# csum: 第一个字段的校验和，用于快速查找是否有相同的笔记。
# flags：用户标记。
# data: [guid, mid, mod, usn, tags, flds, flags, data]数组。可用来撤消编辑？

class Note:
    """笔记类"""

    def __init__(self, col, model=None, id=None):
        """
        初始化。

        :param col: 牌组集。
        :param model: 笔记类型，可选，但model和id不能同时为空。
        :param id: 笔记ID，不为空时，根据ID从数据库中加载内容。否则根据牌组集和笔记类型新建一条笔记。
        """
        assert not (model and id)
        self.col = col
        if id:
            self.id = id
            self.load()
        else:
            self.id = timestampID(col.db, "notes")
            self.guid = guid64()
            self._model = model
            self.mid = model['id']
            self.tags = []
            self.fields = [""] * len(self._model['flds'])
            self.flags = 0
            self.data = ""
            self._fmap = self.col.models.fieldMap(self._model)
            self.scm = self.col.scm

    def load(self):
        """从数据库中加载卡片数据。"""

        (self.guid,
         self.mid,
         self.mod,
         self.usn,
         self.tags,
         self.fields,
         self.flags,
         self.data) = self.col.db.first("""
select guid, mid, mod, usn, tags, flds, flags, data
from notes where id = ?""", self.id)
        self.fields = splitFields(self.fields)
        self.tags = self.col.tags.split(self.tags)
        self._model = self.col.models.get(self.mid)
        self._fmap = self.col.models.fieldMap(self._model)
        self.scm = self.col.scm

    def flush(self, mod=None):
        """如果笔记的字段或标签被修改了，保存回数据库中。"""

        "If fields or tags have changed, write changes to disk."
        assert self.scm == self.col.scm
        self._preFlush()

        # 获取排序字段的内容
        sfld = stripHTMLMedia(self.fields[self.col.models.sortIdx(self._model)])
        tags = self.stringTags()
        fields = self.joinedFields()

        # 如果标签和字段都没有变化直接返回。
        if not mod and self.col.db.scalar(
            "select 1 from notes where id = ? and tags = ? and flds = ?",
            self.id, tags, fields):
            return

        # 保存到数据库中。
        csum = fieldChecksum(self.fields[0])
        self.mod = mod if mod else intTime()
        self.usn = self.col.usn()
        res = self.col.db.execute("""
insert or replace into notes values (?,?,?,?,?,?,?,?,?,?,?)""",
                            self.id, self.guid, self.mid,
                            self.mod, self.usn, tags,
                            fields, sfld, csum, self.flags,
                            self.data)

        # 将标签添加进牌组集的标签中。
        self.col.tags.register(self.tags)

        # 生成卡片
        self._postFlush()

    def joinedFields(self):
        """用\x1f字符连接列表中的元素，并返回列表的字符串。"""
        return joinFields(self.fields)

    def cards(self):
        """返回这个笔记的所有卡片。"""
        return [self.col.getCard(id) for id in self.col.db.list(
            "select id from cards where nid = ? order by ord", self.id)]

    def model(self):
        """返回笔记的笔记类型。"""
        return self._model

    # Dict interface
    ##################################################

    def keys(self):
        """返加字段的名字列表。"""
        return list(self._fmap.keys())

    def values(self):
        return self.fields

    def items(self):
        """返回(字段名，字段内容)的列表。"""
        return [(f['name'], self.fields[ord])
                for ord, f in sorted(self._fmap.values())]

    def _fieldOrd(self, key):
        """根据字段的名字返回字段的索引"""
        try:
            return self._fmap[key][0]
        except:
            raise KeyError(key)

    def __getitem__(self, key):
        """返回字段内容。"""
        return self.fields[self._fieldOrd(key)]

    def __setitem__(self, key, value):
        """设置字段的内容。"""
        self.fields[self._fieldOrd(key)] = value

    def __contains__(self, key):
        """是否包含指定名字的字段。"""
        return key in list(self._fmap.keys())

    # Tags
    ##################################################

    def hasTag(self, tag):
        """
        卡片是否包含指定的标签。

        :param tag:要查找的标签。
        :return: True包含此标签，否则没有。
        """
        return self.col.tags.inList(tag, self.tags)

    def stringTags(self):
        """返回标签字符串。"""
        return self.col.tags.join(self.col.tags.canonify(self.tags))

    def setTagsFromStr(self, str):
        """用字符串设置标签。"""
        self.tags = self.col.tags.split(str)

    def delTag(self, tag):
        """删除标签。"""
        rem = []
        for t in self.tags:
            if t.lower() == tag.lower():
                rem.append(t)
        for r in rem:
            self.tags.remove(r)

    def addTag(self, tag):
        """添加标签。"""
        # duplicates will be stripped on save
        self.tags.append(tag)

    # Unique/duplicate check
    ##################################################

    def dupeOrEmpty(self):
        """
        检查笔记是否有重复或空。

        :return: 1 第一个字段是空的；2 第一个字段有重复。False其它。
       """

        "1 if first is empty; 2 if first is a duplicate, False otherwise."

        # 检查第一字段是否为空。
        val = self.fields[0]
        if not val.strip():
            return 1

        # 计算第一个字段的校验和，然后查找同一笔记类型下是否有相同校验和的笔记。
        # 如果有，则进一步比较第一个字段是否相同。
        csum = fieldChecksum(val)
        # find any matching csums and compare
        for flds in self.col.db.list(
            "select flds from notes where csum = ? and id != ? and mid = ?",
            csum, self.id or 0, self.mid):
            if stripHTMLMedia(
                splitFields(flds)[0]) == stripHTMLMedia(self.fields[0]):
                return 2
        return False

    # Flushing cloze notes
    ##################################################

    def _preFlush(self):
        """
        回存之前要做的事。

        创建后是否有保存的数据库。
        """
        # have we been added yet?
        self.newlyAdded = not self.col.db.scalar(
            "select 1 from cards where nid = ?", self.id)

    def _postFlush(self):
        """
        回存之后要做的事。

        如果是第一次保存，那么产生这条笔记的所有卡片。
       """
        # generate missing cards
        if not self.newlyAdded:
            rem = self.col.genCards([self.id])
            # popping up a dialog while editing is confusing; instead we can
            # document that the user should open the templates window to
            # garbage collect empty cards
            #self.col.remEmptyCards(ids)
