# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import copy, operator
from anki.utils import intTime, ids2str, json
from anki.hooks import runHook
from anki.consts import *
from anki.lang import _
from anki.errors import DeckRenameError

# fixmes:
# - make sure users can't set grad interval < 1

# 牌组设置值。
defaultDeck = {
    'newToday': [0, 0], # currentDay, count
    'revToday': [0, 0],
    'lrnToday': [0, 0],
    'timeToday': [0, 0], # time in ms
    'conf': 1,
    'usn': 0,
    'desc': "",
    'dyn': 0,  # anki uses int/bool interchangably here
    'collapsed': False,
    # added in beta11
    'extendNew': 10,
    'extendRev': 50,
}

# 临时牌组默认值。
defaultDynamicDeck = {
    'newToday': [0, 0],
    'revToday': [0, 0],
    'lrnToday': [0, 0],
    'timeToday': [0, 0],
    'collapsed': False,
    'dyn': 1,
    'desc': "",
    'usn': 0,
    'delays': None,
    'separate': True,
     # list of (search, limit, order); we only use first two elements for now
    'terms': [["", 100, 0]],
    'resched': True,
    'return': True, # currently unused

    # v2 scheduler
    "previewDelay": 10,
}

# 牌组设置默认值。
defaultConf = {
    'name': _("Default"),
    'new': {
        'delays': [1, 10],
        'ints': [1, 4, 7], # 7 is not currently used
        'initialFactor': STARTING_FACTOR,
        'separate': True,
        'order': NEW_CARDS_DUE,
        'perDay': 20,
        # may not be set on old decks
        'bury': False,
    },
    'lapse': {
        'delays': [10],
        'mult': 0,
        'minInt': 1,
        'leechFails': 8,
        # type 0=suspend, 1=tagonly
        'leechAction': 0,
    },
    'rev': {
        'perDay': 200,
        'ease4': 1.3,
        'fuzz': 0.05,
        'minSpace': 1, # not currently used
        'ivlFct': 1,
        'maxIvl': 36500,
        # may not be set on old decks
        'bury': False,
        'hardFactor': 1.2,
    },
    'maxTaken': 60,
    'timer': 0,
    'autoplay': True,
    'replayq': True,
    'mod': 0,
    'usn': 0,
}

class DeckManager:
    """牌组管理"""

    # Registry save/load
    #############################################################

    def __init__(self, col):
        self.col = col

    def load(self, decks, dconf):
        """
        初始化。

        :param decks: 牌组数据，json模式的字符串。
        :param dconf: 牌组设置数据，json模式的字符串。
        :return: 无。
        """
        self.decks = json.loads(decks)
        self.dconf = json.loads(dconf)

        # 限制每日学习和每日复习的最大值。
        # set limits to within bounds
        found = False
        for c in list(self.dconf.values()):
            for t in ('rev', 'new'):
                pd = 'perDay'
                if c[t][pd] > 999999:
                    c[t][pd] = 999999
                    self.save(c)
                    found = True
        if not found:
            self.changed = False

    def save(self, g=None):
        """标记被修改。"""
        "Can be called with either a deck or a deck configuration."
        if g:
            g['mod'] = intTime()
            g['usn'] = self.col.usn()
        self.changed = True

    def flush(self):
        """将牌组和牌组设置数据回存的数据库中。"""
        if self.changed:
            self.col.db.execute("update col set decks=?, dconf=?",
                                 json.dumps(self.decks),
                                 json.dumps(self.dconf))
            self.changed = False

    # Deck save/load
    #############################################################

    def id(self, name, create=True, type=defaultDeck):
        """
        根据牌组名获取牌组ID。

        :param name: 牌组名字。
        :param create: True=如果不存在时，创建名字为name的牌组。
        :param type: 创建牌组时使用的默认值。
        :return: 返回名字为name的牌组ID。
        """

        "Add a deck with NAME. Reuse deck if already exists. Return id as int."

        # 从现有的牌组中找出名字为name的牌组，并返回牌组ID。
        name = name.replace('"', '')
        for id, g in list(self.decks.items()):
            if g['name'].lower() == name.lower():
                return int(id)
        if not create:
            return None

        # 下面要创建新的牌组，先用默认值填充。
        g = copy.deepcopy(type)

        # 如果名字中有::分隔，确保父牌组都存在。
        if "::" in name:
            # not top level; ensure all parents exist
            name = self._ensureParents(name)

        # 设置牌组名字。
        g['name'] = name

        # 设置牌组ID。
        while 1:
            id = intTime(1000)
            if str(id) not in self.decks:
                break
        g['id'] = id

        # 添加保存。
        self.decks[str(id)] = g
        self.save(g)

        # 刷新活动牌组。
        self.maybeAddToActive()
        runHook("newDeck")
        return int(id)

    def rem(self, did, cardsToo=False, childrenToo=True):
        """
        删除牌组。

        :param did: 要删除的牌组ID。
        :param cardsToo: True=删除牌组下的卡片。
        :param childrenToo: True=子牌组也删除。
        :return:
        """

        "Remove the deck. If cardsToo, delete any cards inside."
        if str(did) == '1':
            # 牌组ID=1是默认牌组。默认牌组不充许删除，但如果默认牌组
            # 是一个子牌组，那么会给默认牌组改名。
            # 例如：默认牌组名为 xxx::xxx::abc时改名为abc1，如果
            # 牌组名abc1已经被占用，就改为abc11，直到名字可用为止。
            #
            # we won't allow the default deck to be deleted, but if it's a
            # child of an existing deck then it needs to be renamed
            deck = self.get(did)
            if '::' in deck['name']:
                base = deck['name'].split("::")[-1]
                suffix = ""
                while True:
                    # find an unused name
                    name = base + suffix
                    if not self.byName(name):
                        deck['name'] = name
                        self.save(deck)
                        break
                    suffix += "1"
            return
        # log the removal regardless of whether we have the deck or not
        self.col._logRem([did], REM_DECK)
        # do nothing else if doesn't exist
        if not str(did) in self.decks:
            return
        deck = self.get(did)
        if deck['dyn']:
            # 如果删除的是临时牌组，那么把这个牌组中的卡片发还到原来的牌组中
            # 而不是删除卡片。
            #
            # deleting a cramming deck returns cards to their previous deck
            # rather than deleting the cards
            self.col.sched.emptyDyn(did)
            if childrenToo:
                for name, id in self.children(did):
                    # 递归删除子牌组。
                    self.rem(id, cardsToo)
        else:
            # 递归删除子牌组。
            # delete children first
            if childrenToo:
                # we don't want to delete children when syncing
                for name, id in self.children(did):
                    self.rem(id, cardsToo)

            # 删除牌组下的卡片，包括笔记。
            # delete cards too?
            if cardsToo:
                # don't use cids(), as we want cards in cram decks too
                cids = self.col.db.list(
                    "select id from cards where did=? or odid=?", did, did)
                self.col.remCards(cids)

        # 删除牌组本身。
        # delete the deck and add a grave
        del self.decks[str(did)]

        # 如果删除的牌组是活动牌组之一，重新设置当前牌组和活动牌组。
        # ensure we have an active deck
        if did in self.active():
            self.select(int(list(self.decks.keys())[0]))

        # 标记被修改。
        self.save()

    def allNames(self, dyn=True):
        """
        返回未排序的所有牌组名。

        :param dyn: =True表示也包括临时牌组名。
        :return: 返回未排序的所有牌组名。
        """
        "An unsorted list of all deck names."
        if dyn:
            return [x['name'] for x in list(self.decks.values())]
        else:
            return [x['name'] for x in list(self.decks.values()) if not x['dyn']]

    def all(self):
        """返回所有牌组数据的列表。"""
        "A list of all decks."
        return list(self.decks.values())

    def allIds(self):
        """返回所有牌组ID列表。"""
        return list(self.decks.keys())

    def collapse(self, did):
        """折叠或展开牌组。"""
        deck = self.get(did)
        deck['collapsed'] = not deck['collapsed']
        self.save(deck)

    def collapseBrowser(self, did):
        """在卡片浏览界面折叠或展开牌组。"""
        deck = self.get(did)
        collapsed = deck.get('browserCollapsed', False)
        deck['browserCollapsed'] = not collapsed
        self.save(deck)

    def count(self):
        """返回牌组总数。"""
        return len(self.decks)

    def get(self, did, default=True):
        """
        根据牌组ID获取牌组。

        :param did: 牌组ID
        :param default: =True表示当牌组ID无效时返回默认牌组ID。
        :return: 返回牌组数据。
        """
        """"""
        id = str(did)
        if id in self.decks:
            return self.decks[id]
        elif default:
            return self.decks['1']

    def byName(self, name):
        """根据名字获取牌组。"""
        "Get deck with NAME."
        for m in list(self.decks.values()):
            if m['name'] == name:
                return m

    def update(self, g):
        """
        添加或修改已经存在的牌组，用于同步和合并。
        :param g: 要添加或合并的牌组
        :return: 无
        """
        "Add or update an existing deck. Used for syncing and merging."
        self.decks[str(g['id'])] = g
        self.maybeAddToActive()
        # mark registry changed, but don't bump mod time
        self.save()

    def rename(self, g, newName):
        """修改牌组的名字。"""

        "Rename deck prefix to NAME if not exists. Updates children."

        # 先确认新名字不会重名。
        # make sure target node doesn't already exist
        if newName in self.allNames():
            raise DeckRenameError(_("That deck already exists."))

        # 父牌组中不能有临时牌组。
        # make sure we're not nesting under a filtered deck
        for p in self.parentsByName(newName):
            if p['dyn']:
                raise DeckRenameError(_("A filtered deck cannot have subdecks."))

        # 确保父牌组都存在。
        # ensure we have parents
        newName = self._ensureParents(newName)

        # 修改子牌组的名字。
        # rename children
        for grp in self.all():
            if grp['name'].startswith(g['name'] + "::"):
                grp['name'] = grp['name'].replace(g['name']+ "::",
                                                  newName + "::", 1)
                self.save(grp)

        # 修改自身的名字
        # adjust name
        g['name'] = newName

        # 再次确保父牌组都存在。
        # ensure we have parents again, as we may have renamed parent->child
        newName = self._ensureParents(newName)

        # 标记被修改。
        self.save(g)

        # 刷新活动牌组。
        # renaming may have altered active did order
        self.maybeAddToActive()

    def renameForDragAndDrop(self, draggedDeckDid, ontoDeckDid):
        """
        用于拖放的重命名。

        :param draggedDeckDid: 被拖动的牌组。
        :param ontoDeckDid: 放在哪一个牌组上。
        :return: 无
        """
        draggedDeck = self.get(draggedDeckDid)
        draggedDeckName = draggedDeck['name']
        ontoDeckName = self.get(ontoDeckDid)['name']

        if ontoDeckDid is None or ontoDeckDid == '':
            if len(self._path(draggedDeckName)) > 1:
                self.rename(draggedDeck, self._basename(draggedDeckName))
        elif self._canDragAndDrop(draggedDeckName, ontoDeckName):
            draggedDeck = self.get(draggedDeckDid)
            draggedDeckName = draggedDeck['name']
            ontoDeckName = self.get(ontoDeckDid)['name']
            assert ontoDeckName.strip()
            self.rename(draggedDeck, ontoDeckName + "::" + self._basename(draggedDeckName))

    def _canDragAndDrop(self, draggedDeckName, ontoDeckName):
        """是否能够拖放。"""
        if draggedDeckName == ontoDeckName \
                or self._isParent(ontoDeckName, draggedDeckName) \
                or self._isAncestor(draggedDeckName, ontoDeckName):
                    return False
        else:
            return True

    def _isParent(self, parentDeckName, childDeckName):
        """两个牌组是否为父子关系。"""
        return self._path(childDeckName) == self._path(parentDeckName) + [ self._basename(childDeckName) ]

    def _isAncestor(self, ancestorDeckName, descendantDeckName):
        """两个牌组是否为祖孙关系。"""
        ancestorPath = self._path(ancestorDeckName)
        return ancestorPath == self._path(descendantDeckName)[0:len(ancestorPath)]

    def _path(self, name):
        """牌组名转成数组。"""
        return name.split("::")

    def _basename(self, name):
        """不带路径的牌组名。"""
        return self._path(name)[-1]

    def _ensureParents(self, name):
        """
        确保牌组名字中的父牌组都存在，不存在就创建。

        :param name: 名字
        :return: 牌组名。
        """

        "Ensure parents exist, and return name with case matching parents."
        s = ""
        path = self._path(name)
        if len(path) < 2:
            return name
        for p in path[:-1]:
            if not s:
                s += p
            else:
                s += "::" + p
            # fetch or create
            did = self.id(s)
            # get original case
            s = self.name(did)
        name = s + "::" + path[-1]
        return name

    # Deck configurations
    #############################################################

    def allConf(self):
        """获取所有牌组设置。"""
        "A list of all deck config."
        return list(self.dconf.values())

    def confForDid(self, did):
        """获取指定牌组设置。"""
        deck = self.get(did, default=False)
        assert deck
        if 'conf' in deck:
            conf = self.getConf(deck['conf'])
            conf['dyn'] = False
            return conf
        # dynamic decks have embedded conf
        return deck

    def getConf(self, confId):
        """根据牌组设置ID获取牌组设置。"""
        return self.dconf[str(confId)]

    def updateConf(self, g):
        """修改或添加牌组设置。"""
        self.dconf[str(g['id'])] = g
        self.save()

    def confId(self, name, cloneFrom=defaultConf):
        """
        复制牌组设置做为新的牌组设置

        :param name: 牌组设置的名字。
        :param cloneFrom: 要复制的牌组设置。
        :return: 新的牌组设置的ID。
        """

        "Create a new configuration and return id."
        c = copy.deepcopy(cloneFrom)
        # 为新的牌组设置分配ID。
        while 1:
            id = intTime(1000)
            if str(id) not in self.dconf:
                break
        c['id'] = id
        c['name'] = name
        self.dconf[str(id)] = c
        self.save(c)
        return id

    def remConf(self, id):
        """删除牌组设置，使用这个牌组设置的牌组使用默认牌组设置。"""
        "Remove a configuration and update all decks using it."
        assert int(id) != 1
        self.col.modSchema(check=True)
        del self.dconf[str(id)]
        for g in self.all():
            # ignore cram decks
            if 'conf' not in g:
                continue
            if str(g['conf']) == str(id):
                g['conf'] = 1
                self.save(g)

    def setConf(self, grp, id):
        """设置牌组使用的牌组设置。"""
        grp['conf'] = id
        self.save(grp)

    def didsForConf(self, conf):
        """返回使用这个牌组设置的所有牌组ID。"""
        dids = []
        for deck in list(self.decks.values()):
            if 'conf' in deck and deck['conf'] == conf['id']:
                dids.append(deck['id'])
        return dids

    def restoreToDefault(self, conf):
        """恢得牌组设置成默认值。"""
        oldOrder = conf['new']['order']
        new = copy.deepcopy(defaultConf)
        new['id'] = conf['id']
        new['name'] = conf['name']
        self.dconf[str(conf['id'])] = new
        self.save(new)

        # 如果之前牌组设置中的新卡片顺序设置为随机的，
        # 那么这里要重新按设置中的方式对新卡片排序。
        # if it was previously randomized, resort
        if not oldOrder:
            self.col.sched.resortConf(new)

    # Deck utils
    #############################################################

    def name(self, did, default=False):
        """根据牌组ID，获取牌组名字。"""
        deck = self.get(did, default=default)
        if deck:
            return deck['name']
        return _("[no deck]")

    def nameOrNone(self, did):
        """根据牌组ID，获取牌组名字。牌组不存在时返回None。"""
        deck = self.get(did, default=False)
        if deck:
            return deck['name']
        return None

    def setDeck(self, cids, did):
        """为卡片设置牌组。"""
        self.col.db.execute(
            "update cards set did=?,usn=?,mod=? where id in "+
            ids2str(cids), did, self.col.usn(), intTime())

    def maybeAddToActive(self):
        """刷新活动牌组。"""
        # reselect current deck, or default if current has disappeared
        c = self.current()
        self.select(c['id'])

    def cids(self, did, children=False):
        """
        返回牌组下所有的卡片ID列表。

        :param did: 牌组ID。
        :param children: =True时，也包括子牌组下的卡片ID。
        :return: 卡片ID列表。
        """
        if not children:
            return self.col.db.list("select id from cards where did=?", did)
        dids = [did]
        for name, id in self.children(did):
            dids.append(id)
        return self.col.db.list("select id from cards where did in "+
                                ids2str(dids))

    def recoverOrphans(self):
        """如果卡片的牌组ID无效，将卡片的牌组ID改为1。"""
        dids = list(self.decks.keys())
        mod = self.col.db.mod
        self.col.db.execute("update cards set did = 1 where did not in "+
                            ids2str(dids))
        self.col.db.mod = mod

    # Deck selection
    #############################################################

    def active(self):
        """获得所有活动牌组ID的数组。活动牌组包括当前用户选择的牌组和它的子牌组。"""
        "The currrently active dids. Make sure to copy before modifying."
        return self.col.conf['activeDecks']

    def selected(self):
        """获得当前选择的牌组ID。"""
        "The currently selected did."
        return self.col.conf['curDeck']

    def current(self):
        """获得当前牌组。"""
        return self.get(self.selected())

    def select(self, did):
        """根据牌组ID选择一个牌组分支，这个分支上的所有牌组为活动牌组，被选择的牌组为当前牌组。"""
        "Select a new branch."
        # make sure arg is an int
        did = int(did)
        # current deck
        self.col.conf['curDeck'] = did
        # and active decks (current + all children)
        actv = self.children(did)
        actv.sort()
        self.col.conf['activeDecks'] = [did] + [a[1] for a in actv]
        self.changed = True

    def children(self, did):
        """根据牌组ID获取所有的子牌组的(name, id)。"""
        "All children of did, as (name, id)."
        name = self.get(did)['name']
        actv = []
        for g in self.all():
            if g['name'].startswith(name + "::"):
                actv.append((g['name'], g['id']))
        return actv

    def childDids(self, did, childMap):
        def gather(node, arr):
            for did, child in node.items():
                arr.append(did)
                gather(child, arr)

        arr = []
        gather(childMap[did], arr)
        return arr

    def childMap(self):
        nameMap = self.nameMap()
        childMap = {}

        # go through all decks, sorted by name
        for deck in sorted(self.all(), key=operator.itemgetter("name")):
            node = {}
            childMap[deck['id']] = node

            # add note to immediate parent
            parts = deck['name'].split("::")
            if len(parts) > 1:
                immediateParent = "::".join(parts[:-1])
                pid = nameMap[immediateParent]['id']
                childMap[pid][deck['id']] = node

        return childMap

    def parents(self, did, nameMap=None):
        """根据牌组ID获取它的所有父牌组。"""
        "All parents of did."
        # get parent and grandparent names
        parents = []
        for part in self.get(did)['name'].split("::")[:-1]:
            if not parents:
                parents.append(part)
            else:
                parents.append(parents[-1] + "::" + part)
        # convert to objects
        for c, p in enumerate(parents):
            if nameMap:
                deck = nameMap[p]
            else:
                deck = self.get(self.id(p))
            parents[c] = deck
        return parents

    def parentsByName(self, name):
        """返回所有父牌组列表。"""
        "All existing parents of name"
        if "::" not in name:
            return []
        names = name.split("::")[:-1]
        head = []
        parents = []

        while names:
            head.append(names.pop(0))
            deck = self.byName("::".join(head))
            if deck:
                parents.append(deck)

        return parents

    def nameMap(self):
        return dict((d['name'], d) for d in self.decks.values())

    # Sync handling
    ##########################################################################

    def beforeUpload(self):
        for d in self.all():
            d['usn'] = 0
        for c in self.allConf():
            c['usn'] = 0
        self.save()

    # Dynamic decks
    ##########################################################################

    def newDyn(self, name):
        "Return a new dynamic deck and set it as the current deck."
        did = self.id(name, type=defaultDynamicDeck)
        self.select(did)
        return did

    def isDyn(self, did):
        return self.get(did)['dyn']
