# -*- coding: utf-8 -*-
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pprint
import re
import time
import os
import random
import stat
import datetime
import copy
import traceback

from anki.lang import _, ngettext
from anki.utils import ids2str, fieldChecksum, stripHTML, \
    intTime, splitFields, joinFields, maxID, json, devMode, stripHTMLMedia
from anki.hooks import  runFilter, runHook
from anki.models import ModelManager
from anki.media import MediaManager
from anki.decks import DeckManager
from anki.tags import TagManager
from anki.consts import *
from anki.errors import AnkiError
from anki.sound import stripSounds
import anki.latex # sets up hook
import anki.cards
import anki.notes
import anki.template
import anki.find


defaultConf = {
    # review options
    'activeDecks': [1],
    'curDeck': 1,
    'newSpread': NEW_CARDS_DISTRIBUTE,
    'collapseTime': 1200,
    'timeLim': 0,
    'estTimes': True,
    'dueCounts': True,
    # other config
    'curModel': None,
    'nextPos': 1,
    'sortType': "noteFld",
    'sortBackwards': False,
    'addToCur': True, # add new to currently selected deck?
    'dayLearnFirst': False,
}

# this is initialized by storage.Collection
class _Collection:

    def __init__(self, db, server=False, log=False):
        self._debugLog = log
        self.db = db
        self.path = db._path
        self._openLog()
        self.log(self.path, anki.version)
        self.server = server
        self._lastSave = time.time()
        self.clearUndo()
        self.media = MediaManager(self, server)
        self.models = ModelManager(self)
        self.decks = DeckManager(self)
        self.tags = TagManager(self)
        self.load()
        if not self.crt:
            d = datetime.datetime.today()
            d -= datetime.timedelta(hours=4)
            d = datetime.datetime(d.year, d.month, d.day)
            d += datetime.timedelta(hours=4)
            self.crt = int(time.mktime(d.timetuple()))
        self._loadScheduler()
        if not self.conf.get("newBury", False):
            self.conf['newBury'] = True
            self.setMod()

    def name(self):
        n = os.path.splitext(os.path.basename(self.path))[0]
        return n

    # Scheduler
    ##########################################################################

    defaultSchedulerVersion = 1
    supportedSchedulerVersions = (1, 2)

    # 得到使用的排期器版本。
    def schedVer(self):
        ver = self.conf.get("schedVer", self.defaultSchedulerVersion)
        if ver in self.supportedSchedulerVersions:
            return ver
        else:
            raise Exception("Unsupported scheduler version")

    def _loadScheduler(self):
        ver = self.schedVer()
        if ver == 1:
            from anki.sched import Scheduler
        elif ver == 2:
            from anki.schedv2 import Scheduler

        self.sched = Scheduler(self)

    # 切换排期器版本。
    def changeSchedulerVer(self, ver):
        if ver == self.schedVer():
            return
        if ver not in self.supportedSchedulerVersions:
            raise Exception("Unsupported scheduler version")

        self.modSchema(check=True)

        from anki.schedv2 import Scheduler
        v2Sched = Scheduler(self)

        if ver == 1:
            v2Sched.moveToV1()
        else:
            v2Sched.moveToV2()

        self.conf['schedVer'] = ver
        self.setMod()

        self._loadScheduler()

    # DB-related
    ##########################################################################

    # 从数据库中加载数据。
    def load(self):
        (self.crt,
         self.mod,
         self.scm,
         self.dty, # no longer used
         self._usn,
         self.ls,
         self.conf,
         models,
         decks,
         dconf,
         tags) = self.db.first("""
select crt, mod, scm, dty, usn, ls,
conf, models, decks, dconf, tags from col""")
        self.conf = json.loads(self.conf)
        self.models.load(models)
        self.decks.load(decks, dconf)
        self.tags.load(tags)

    # 标记数据库有改动。
    def setMod(self):
        """Mark DB modified.

DB operations and the deck/tag/model managers do this automatically, so this
is only necessary if you modify properties of this object or the conf dict."""
        self.db.mod = True

    def flush(self, mod=None):
        "Flush state to DB, updating mod time."
        self.mod = intTime(1000) if mod is None else mod
        self.db.execute(
            """update col set
crt=?, mod=?, scm=?, dty=?, usn=?, ls=?, conf=?""",
            self.crt, self.mod, self.scm, self.dty,
            self._usn, self.ls, json.dumps(self.conf))

    def save(self, name=None, mod=None):
        "Flush, commit DB, and take out another write lock."
        # let the managers conditionally flush
        self.models.flush()
        self.decks.flush()
        self.tags.flush()
        # and flush deck + bump mod if db has been changed
        if self.db.mod:
            self.flush(mod=mod)
            self.db.commit()
            self.lock()
            self.db.mod = False
        self._markOp(name)
        self._lastSave = time.time()

    def autosave(self):
        "Save if 5 minutes has passed since last save. True if saved."
        if time.time() - self._lastSave > 300:
            self.save()
            return True

    def lock(self):
        # make sure we don't accidentally bump mod time
        mod = self.db.mod
        self.db.execute("update col set mod=mod")
        self.db.mod = mod

    def close(self, save=True):
        "Disconnect from DB."
        if self.db:
            if save:
                self.save()
            else:
                self.db.rollback()
            if not self.server:
                self.db.setAutocommit(True)
                self.db.execute("pragma journal_mode = delete")
                self.db.setAutocommit(False)
            self.db.close()
            self.db = None
            self.media.close()
            self._closeLog()

    def reopen(self):
        "Reconnect to DB (after changing threads, etc)."
        import anki.db
        if not self.db:
            self.db = anki.db.DB(self.path)
            self.media.connect()
            self._openLog()

    def rollback(self):
        self.db.rollback()
        self.load()
        self.lock()

    def modSchema(self, check):
        "Mark schema modified. Call this first so user can abort if necessary."
        if not self.schemaChanged():
            if check and not runFilter("modSchema", True):
                raise AnkiError("abortSchemaMod")
        self.scm = intTime(1000)
        self.setMod()

    def schemaChanged(self):
        "True if schema changed since last sync."
        return self.scm > self.ls

    def usn(self):
        """
        获取序列号

        用于数据同步，表示哪些数据已经上传过，哪些没有。
        每次完整上传后值加1.
        """
        return self._usn if self.server else -1

    def beforeUpload(self):
        "Called before a full upload."
        tbls = "notes", "cards", "revlog"
        for t in tbls:
            self.db.execute("update %s set usn=0 where usn=-1" % t)
        # we can save space by removing the log of deletions
        self.db.execute("delete from graves")
        self._usn += 1
        self.models.beforeUpload()
        self.tags.beforeUpload()
        self.decks.beforeUpload()
        self.modSchema(check=False)
        self.ls = self.scm
        # ensure db is compacted before upload
        self.db.setAutocommit(True)
        self.db.execute("vacuum")
        self.db.execute("analyze")
        self.close()

    # Object creation helpers
    ##########################################################################

    def getCard(self, id):
        return anki.cards.Card(self, id)

    def getNote(self, id):
        return anki.notes.Note(self, id=id)

    # Utils
    ##########################################################################

    def nextID(self, type, inc=True):
        """
        返回下一个ID值。

        :param type: ID的类型。
        :param inc: True=下一个ID值加1。默认为True。
        :return: 返回下一个ID值，ID值从1开始。
        """
        type = "next"+type.capitalize()
        id = self.conf.get(type, 1)
        if inc:
            self.conf[type] = id+1
        return id

    def reset(self):
        "Rebuild the queue and reload data after DB modified."
        self.sched.reset()

    # Deletion logging
    ##########################################################################

    def _logRem(self, ids, type):
        self.db.executemany("insert into graves values (%d, ?, %d)" % (
            self.usn(), type), ([x] for x in ids))

    # Notes
    ##########################################################################

    def noteCount(self):
        return self.db.scalar("select count() from notes")

    def newNote(self, forDeck=True):
        "Return a new note with the current model."
        return anki.notes.Note(self, self.models.current(forDeck))

    def addNote(self, note):
        "Add a note to the collection. Return number of new cards."
        # check we have card models available, then save
        cms = self.findTemplates(note)
        if not cms:
            return 0
        note.flush()
        # deck conf governs which of these are used
        due = self.nextID("pos")
        # add cards
        ncards = 0
        for template in cms:
            self._newCard(note, template, due)
            ncards += 1
        return ncards

    def remNotes(self, ids):
        self.remCards(self.db.list("select id from cards where nid in "+
                                   ids2str(ids)))

    def _remNotes(self, ids):
        "Bulk delete notes by ID. Don't call this directly."
        if not ids:
            return
        strids = ids2str(ids)
        # we need to log these independently of cards, as one side may have
        # more card templates
        runHook("remNotes", self, ids)
        self._logRem(ids, REM_NOTE)
        self.db.execute("delete from notes where id in %s" % strids)

    # Card creation
    ##########################################################################

    def findTemplates(self, note):
        "Return (active), non-empty templates."
        model = note.model()
        avail = self.models.availOrds(model, joinFields(note.fields))
        return self._tmplsFromOrds(model, avail)

    def _tmplsFromOrds(self, model, avail):
        """
        根据笔记类型model中可用的卡片模板ID列表avail，返回avail的卡片模板列表。

        :param model: 笔记类型
        :param avail: 卡片模板ID列表
        :return: 卡片模板列表
        """

        ok = []
        if model['type'] == MODEL_STD:
            # 标准型的模板类型，根据卡片模板ID索引到卡片模板。
            for t in model['tmpls']:
                if t['ord'] in avail:
                    ok.append(t)
        else:
            # 填空型的模板类型，只有一个卡片模板，但有不同的ord。
            # 这时的ord不表示卡片模板索引，而是表示对卡片模板中的
            # 哪种{{c数字::文字}}要使用填空功能。
            #
            # 例如这时ord=2时，表示要对卡片模板中的所有{{c3::文字}}
            # 使用填空功能；ord=0时，表示要对卡片模板中的所有
            # {{c1::文字}}使用填空功能。
            #
            # 通过这种方式来"虚拟"出不同的卡片模板。

            # cloze - generate temporary templates from first
            for ord in avail:
                t = copy.copy(model['tmpls'][0])
                t['ord'] = ord
                ok.append(t)
        return ok

    def genCards(self, nids):
        """
        产生笔记下的所有卡片。

        :param nids: 笔记ID列表。
        :return: 返回这些笔记下需要删除的卡片ID列表。
        """

        "Generate cards for non-empty templates, return ids to remove."
        # build map of (nid,ord) so we don't create dupes
        snids = ids2str(nids)
        have = {}
        dids = {}
        dues = {}

        # 遍历这些笔记下的所有卡片。
        for id, nid, ord, did, due, odue, odid in self.db.execute(
            "select id, nid, ord, did, due, odue, odid from cards where nid in "+snids):

            # have[笔记ID][模板ID] = 卡片ID
            # have记录了：已经存在的这些卡片使用了哪个笔记中的哪个卡片模板。
            # 这样在后面就可以通过判断have[笔记ID][模板ID]是否存在来检查
            # 某个笔记中的某个卡片模板是否有对应的卡片。
            # existing cards
            if nid not in have:
                have[nid] = {}
            have[nid][ord] = id

            # 如果卡片在临时牌组中，那么在原来的牌组中新增卡片。
            # if in a filtered deck, add new cards to original deck
            if odid != 0:
                did = odid

            # dids[nid] = None表示笔记nid的所有卡片不在相同牌组中，
            # 否则都在牌组dids[nid]中。
            # and their dids
            if nid in dids:
                if dids[nid] and dids[nid] != did:
                    # 如果同一条笔记下的卡片在不同的牌组中出现？
                    # cards are in two or more different decks; revert to
                    # model default
                    dids[nid] = None
            else:
                # first card or multiple cards in same deck
                dids[nid] = did

            # save due
            if odid != 0:
                due = odue
            if nid not in dues:
                dues[nid] = due

        # 为每条笔记生成卡片。
        # build cards for each note
        data = []
        ts = maxID(self.db)
        now = intTime()
        rem = []
        usn = self.usn()

        # 由于这些笔记的字段可能被修改，又或者笔记使用的卡片模板
        # 被修改，这些修改可能导致字段内容与卡片模板之间的依赖关
        # 系发生变化，导致笔记中出现一些以前没有但现在需要添加的
        # 新卡片或者已经存在但现在无效的卡片需要删除的情况。
        #
        # 所以遍历这些笔记，做两件事：
        #  1. 为笔记添加缺少的卡片。
        #  2. 删除已经存在但现在无效了的卡片。
        #     注意：出于安全考虑，实际并没有删除，只是记录下了这
        #     些卡片，但不做任何处理，因为卡片上有排期数据。
        #     如果用户修改失误，导到某些卡片无效，用户还可以改回来。
        #     真的删除了卡片，卡片的有排期数据就回不来了。
        #
        for nid, mid, flds in self.db.execute(
            "select id, mid, flds from notes where id in "+snids):

            # 笔记的笔记类型
            model = self.models.get(mid)

            # 现在可用的卡片模板
            avail = self.models.availOrds(model, flds)

            # 牌组ID，如果这条笔记下的所有卡片都在同一个牌组，就继续用个牌组的ID。
            # 否则使用笔记类型的所属的牌组ID。
            did = dids.get(nid) or model['did']

            # 到期时间，复制于这条笔记下最先遇到的卡片的到期时间。
            due = dues.get(nid)

            # 遍历这个笔记的可用卡片模板。
            # add any missing cards
            for t in self._tmplsFromOrds(model, avail):

                # 这个笔记的这个卡片模板对应的卡片是否已经存在。
                doHave = nid in have and t['ord'] in have[nid]
                if not doHave:
                    # 不存在，添加这张缺少的卡片，
                    # 这里先记录加新加卡片的数据，后面在批量添加到数据库中。
                    # check deck is not a cram deck
                    did = t['did'] or did
                    if self.decks.isDyn(did):
                        did = 1
                    # if the deck doesn't exist, use default instead
                    did = self.decks.get(did)['id']
                    # use sibling due# if there is one, else use a new id
                    if due is None:
                        due = self.nextID("pos")
                    data.append((ts, nid, did, t['ord'],
                                 now, usn, due))
                    ts += 1

            # 记录已经存在但无效了的卡片，需要删除。
            # note any cards that need removing
            if nid in have:
                for ord, id in list(have[nid].items()):
                    if ord not in avail:
                        rem.append(id)

        # 批量新入新卡片到cards数据表中。
        # bulk update
        self.db.executemany("""
insert into cards values (?,?,?,?,?,?,0,0,?,0,0,0,0,0,0,0,0,"")""",
                            data)

        # 需要删除的卡片ID列表。
        return rem

    # type 0 - when previewing in add dialog, only non-empty
    # type 1 - when previewing edit, only existing
    # type 2 - when previewing in models dialog, all templates
    def previewCards(self, note, type=0):
        if type == 0:
            cms = self.findTemplates(note)
        elif type == 1:
            cms = [c.template() for c in note.cards()]
        else:
            cms = note.model()['tmpls']
        if not cms:
            return []
        cards = []
        for template in cms:
            cards.append(self._newCard(note, template, 1, flush=False))
        return cards

    def _newCard(self, note, template, due, flush=True):
        "Create a new card."
        card = anki.cards.Card(self)
        card.nid = note.id
        card.ord = template['ord']
        # Use template did (deck override) if valid, otherwise model did
        if template['did'] and str(template['did']) in self.decks.decks:
            card.did = template['did']
        else:
            card.did = note.model()['did']
        # if invalid did, use default instead
        deck = self.decks.get(card.did)
        if deck['dyn']:
            # must not be a filtered deck
            card.did = 1
        else:
            card.did = deck['id']
        card.due = self._dueForDid(card.did, due)
        if flush:
            card.flush()
        return card

    def _dueForDid(self, did, due):
        conf = self.decks.confForDid(did)
        # in order due?
        if conf['new']['order'] == NEW_CARDS_DUE:
            return due
        else:
            # random mode; seed with note ts so all cards of this note get the
            # same random number
            r = random.Random()
            r.seed(due)
            return r.randrange(1, max(due, 1000))

    # Cards
    ##########################################################################

    def isEmpty(self):
        return not self.db.scalar("select 1 from cards limit 1")

    def cardCount(self):
        return self.db.scalar("select count() from cards")

    def remCards(self, ids, notes=True):
        """
        删除卡片。

        :param ids: 卡片的ID列表。
        :param notes: True=如果笔记下的卡片被全部删除，那么也删除这条笔记。
        :return: 无。
        """
        "Bulk delete cards by ID."
        if not ids:
            return
        sids = ids2str(ids)
        nids = self.db.list("select nid from cards where id in "+sids)
        # remove cards
        self._logRem(ids, REM_CARD)
        self.db.execute("delete from cards where id in "+sids)
        # then notes
        if not notes:
            return
        nids = self.db.list("""
select id from notes where id in %s and id not in (select nid from cards)""" %
                     ids2str(nids))
        self._remNotes(nids)

    def emptyCids(self):
        rem = []
        for m in self.models.all():
            rem += self.genCards(self.models.nids(m))
        return rem

    def emptyCardReport(self, cids):
        rep = ""
        for ords, cnt, flds in self.db.all("""
select group_concat(ord+1), count(), flds from cards c, notes n
where c.nid = n.id and c.id in %s group by nid""" % ids2str(cids)):
            rep += _("Empty card numbers: %(c)s\nFields: %(f)s\n\n") % dict(
                c=ords, f=flds.replace("\x1f", " / "))
        return rep

    # Field checksums and sorting fields
    ##########################################################################

    def _fieldData(self, snids):
        """返回sndis中的笔记的(笔记ID，笔记类型ID，字段内容)列表。"""
        return self.db.execute(
            "select id, mid, flds from notes where id in "+snids)

    def updateFieldCache(self, nids):
        """
        更新笔记中的用于排序的字段sfld和校验字段csum。

        :param nids: 要更新的笔记ID列表。
        :return: 无。
        """

        "Update field checksums and sort cache, after find&replace, etc."
        snids = ids2str(nids)
        r = []

        # 遍历snids中的所有笔记。
        for (nid, mid, flds) in self._fieldData(snids):

            # 获取这条笔记的字段内容和排序字段的索引。
            # 重新计算这条用于排序的sfld内容和校验。
            fields = splitFields(flds)
            model = self.models.get(mid)
            if not model:
                # note points to invalid model
                continue
            r.append((stripHTMLMedia(fields[self.models.sortIdx(model)]),
                      fieldChecksum(fields[0]),
                      nid))

        # 批量更新notes数据表中的内容。
        # apply, relying on calling code to bump usn+mod
        self.db.executemany("update notes set sfld=?, csum=? where id=?", r)

    # Q/A generation
    ##########################################################################

    # 1.返回某张卡片的、某个笔记的或某个笔记类型的所有 “问题/答案字典”的数组
    # 2.数组的每个元素都是一个字典，字典中['q']是问题的html字串，['a']是答案的
    # html字串。
    # 3. 对于填空型的会检查是否有至少一个cloze:出现。
    def renderQA(self, ids=None, type="card"):
        # gather metadata
        if type == "card":
            where = "and c.id in " + ids2str(ids)
        elif type == "note":
            where = "and f.id in " + ids2str(ids)
        elif type == "model":
            where = "and m.id in " + ids2str(ids)
        elif type == "all":
            where = ""
        else:
            raise Exception()
        return [self._renderQA(row)
                for row in self._qaData(where)]

    def _renderQA(self, data, qfmt=None, afmt=None):
        """
        生成问题和答案的html代码。

        :param data: 构造参数，格式为：[卡片ID、笔记ID、笔记类型ID、牌组ID、卡片模板索引、笔记的标签、笔记的字段]。
        :param qfmt: 不为空时，用于替换问题显示格式。
        :param afmt: 不为空时，用于替换答案显示格式。
        :return: 返回问题和答案的html代码。
        """

        "Returns hash of id, question, answer."
        # data is [cid, nid, mid, did, ord, tags, flds, cardFlags]
        # unpack fields and create dict
        flist = splitFields(data[6])
        fields = {}
        model = self.models.get(data[2])
        for (name, (idx, conf)) in list(self.models.fieldMap(model).items()):
            fields[name] = flist[idx]
        fields['Tags'] = data[5].strip()
        fields['Type'] = model['name']
        fields['Deck'] = self.decks.name(data[3])
        fields['Subdeck'] = fields['Deck'].split('::')[-1]
        fields['CardFlag'] = self._flagNameFromCardFlags(data[7])
        if model['type'] == MODEL_STD:
            template = model['tmpls'][data[4]]
        else:
            template = model['tmpls'][0]
        fields['Card'] = template['name']
        fields['c%d' % (data[4]+1)] = "1"
        # render q & a
        d = dict(id=data[0])
        qfmt = qfmt or template['qfmt']
        afmt = afmt or template['afmt']
        for (type, format) in (("q", qfmt), ("a", afmt)):
            if type == "q":
                format = re.sub("{{(?!type:)(.*?)cloze:", r"{{\1cq-%d:" % (data[4]+1), format)
                format = format.replace("<%cloze:", "<%%cq:%d:" % (
                    data[4]+1))
            else:
                format = re.sub("{{(.*?)cloze:", r"{{\1ca-%d:" % (data[4]+1), format)
                format = format.replace("<%cloze:", "<%%ca:%d:" % (
                    data[4]+1))
                fields['FrontSide'] = stripSounds(d['q'])
            fields = runFilter("mungeFields", fields, model, data, self)
            html = anki.template.render(format, fields)
            d[type] = runFilter(
                "mungeQA", html, type, fields, model, data, self)
            # empty cloze?
            if type == 'q' and model['type'] == MODEL_CLOZE:
                if not self.models._availClozeOrds(model, data[6], False):
                    d['q'] += ("<p>" + _(
                "Please edit this note and add some cloze deletions. (%s)") % (
                "<a href=%s#cloze>%s</a>" % (HELP_SITE, _("help"))))
        return d

    def _qaData(self, where=""):
        "Return [cid, nid, mid, did, ord, tags, flds, cardFlags] db query"
        return self.db.execute("""
select c.id, f.id, f.mid, c.did, c.ord, f.tags, f.flds, c.flags
from cards c, notes f
where c.nid == f.id
%s""" % where)

    def _flagNameFromCardFlags(self, flags):
        flag = flags & 0b111
        if not flag:
            return ""
        return "flag%d" % flag

    # Finding cards
    ##########################################################################

    def findCards(self, query, order=False):
        return anki.find.Finder(self).findCards(query, order)

    def findNotes(self, query):
        return anki.find.Finder(self).findNotes(query)

    def findReplace(self, nids, src, dst, regex=None, field=None, fold=True):
        return anki.find.findReplace(self, nids, src, dst, regex, field, fold)

    def findDupes(self, fieldName, search=""):
        return anki.find.findDupes(self, fieldName, search)

    # Stats
    ##########################################################################

    def cardStats(self, card):
        from anki.stats import CardStats
        return CardStats(self, card).report()

    def stats(self):
        from anki.stats import CollectionStats
        return CollectionStats(self)

    # Timeboxing
    ##########################################################################

    def startTimebox(self):
        self._startTime = time.time()
        self._startReps = self.sched.reps

    def timeboxReached(self):
        "Return (elapsedTime, reps) if timebox reached, or False."
        if not self.conf['timeLim']:
            # timeboxing disabled
            return False
        elapsed = time.time() - self._startTime
        if elapsed > self.conf['timeLim']:
            return (self.conf['timeLim'], self.sched.reps - self._startReps)

    # Undo
    ##########################################################################

    def clearUndo(self):
        # [type, undoName, data]
        # type 1 = review; type 2 = checkpoint
        self._undo = None

    def undoName(self):
        "Undo menu item name, or None if undo unavailable."
        if not self._undo:
            return None
        return self._undo[1]

    def undo(self):
        if self._undo[0] == 1:
            return self._undoReview()
        else:
            self._undoOp()

    def markReview(self, card):
        """
        标记一张卡片为正在复习。
        :param card: 正在复习的卡片
        :return: 无
        """
        old = []
        if self._undo:
            if self._undo[0] == 1:
                old = self._undo[2]
            self.clearUndo()
        wasLeech = card.note().hasTag("leech") or False
        self._undo = [1, _("Review"), old + [copy.copy(card)], wasLeech]

    def _undoReview(self):
        data = self._undo[2]
        wasLeech = self._undo[3]
        c = data.pop()
        if not data:
            self.clearUndo()
        # remove leech tag if it didn't have it before
        if not wasLeech and c.note().hasTag("leech"):
            c.note().delTag("leech")
            c.note().flush()
        # write old data
        c.flush()
        # and delete revlog entry
        last = self.db.scalar(
            "select id from revlog where cid = ? "
            "order by id desc limit 1", c.id)
        self.db.execute("delete from revlog where id = ?", last)
        # restore any siblings
        self.db.execute(
            "update cards set queue=type,mod=?,usn=? where queue=-2 and nid=?",
            intTime(), self.usn(), c.nid)
        # and finally, update daily counts
        n = 1 if c.queue == 3 else c.queue
        type = ("new", "lrn", "rev")[n]
        self.sched._updateStats(c, type, -1)
        self.sched.reps -= 1
        return c.id

    def _markOp(self, name):
        "Call via .save()"
        if name:
            self._undo = [2, name]
        else:
            # saving disables old checkpoint, but not review undo
            if self._undo and self._undo[0] == 2:
                self.clearUndo()

    def _undoOp(self):
        self.rollback()
        self.clearUndo()

    # DB maintenance
    ##########################################################################

    def basicCheck(self):
        "Basic integrity check for syncing. True if ok."
        # cards without notes
        if self.db.scalar("""
select 1 from cards where nid not in (select id from notes) limit 1"""):
            return
        # notes without cards or models
        if self.db.scalar("""
select 1 from notes where id not in (select distinct nid from cards)
or mid not in %s limit 1""" % ids2str(self.models.ids())):
            return
        # invalid ords
        for m in self.models.all():
            # ignore clozes
            if m['type'] != MODEL_STD:
                continue
            if self.db.scalar("""
select 1 from cards where ord not in %s and nid in (
select id from notes where mid = ?) limit 1""" %
                               ids2str([t['ord'] for t in m['tmpls']]),
                               m['id']):
                return
        return True

    def fixIntegrity(self):
        "Fix possible problems and rebuild caches."
        problems = []
        self.save()
        oldSize = os.stat(self.path)[stat.ST_SIZE]
        if self.db.scalar("pragma integrity_check") != "ok":
            return (_("Collection is corrupt. Please see the manual."), False)
        # note types with a missing model
        ids = self.db.list("""
select id from notes where mid not in """ + ids2str(self.models.ids()))
        if ids:
            problems.append(
                ngettext("Deleted %d note with missing note type.",
                         "Deleted %d notes with missing note type.", len(ids))
                         % len(ids))
            self.remNotes(ids)
        # for each model
        for m in self.models.all():
            for t in m['tmpls']:
                if t['did'] == "None":
                    t['did'] = None
                    problems.append(_("Fixed AnkiDroid deck override bug."))
                    self.models.save(m)
            if m['type'] == MODEL_STD:
                # model with missing req specification
                if 'req' not in m:
                    self.models._updateRequired(m)
                    problems.append(_("Fixed note type: %s") % m['name'])
                # cards with invalid ordinal
                ids = self.db.list("""
select id from cards where ord not in %s and nid in (
select id from notes where mid = ?)""" %
                                   ids2str([t['ord'] for t in m['tmpls']]),
                                   m['id'])
                if ids:
                    problems.append(
                        ngettext("Deleted %d card with missing template.",
                                 "Deleted %d cards with missing template.",
                                 len(ids)) % len(ids))
                    self.remCards(ids)
            # notes with invalid field count
            ids = []
            for id, flds in self.db.execute(
                    "select id, flds from notes where mid = ?", m['id']):
                if (flds.count("\x1f") + 1) != len(m['flds']):
                    ids.append(id)
            if ids:
                problems.append(
                    ngettext("Deleted %d note with wrong field count.",
                             "Deleted %d notes with wrong field count.",
                             len(ids)) % len(ids))
                self.remNotes(ids)
        # delete any notes with missing cards
        ids = self.db.list("""
select id from notes where id not in (select distinct nid from cards)""")
        if ids:
            cnt = len(ids)
            problems.append(
                ngettext("Deleted %d note with no cards.",
                         "Deleted %d notes with no cards.", cnt) % cnt)
            self._remNotes(ids)
        # cards with missing notes
        ids = self.db.list("""
select id from cards where nid not in (select id from notes)""")
        if ids:
            cnt = len(ids)
            problems.append(
                ngettext("Deleted %d card with missing note.",
                         "Deleted %d cards with missing note.", cnt) % cnt)
            self.remCards(ids)
        # cards with odue set when it shouldn't be
        ids = self.db.list("""
select id from cards where odue > 0 and (type=1 or queue=2) and not odid""")
        if ids:
            cnt = len(ids)
            problems.append(
                ngettext("Fixed %d card with invalid properties.",
                         "Fixed %d cards with invalid properties.", cnt) % cnt)
            self.db.execute("update cards set odue=0 where id in "+
                ids2str(ids))
        # cards with odid set when not in a dyn deck
        dids = [id for id in self.decks.allIds() if not self.decks.isDyn(id)]
        ids = self.db.list("""
select id from cards where odid > 0 and did in %s""" % ids2str(dids))
        if ids:
            cnt = len(ids)
            problems.append(
                ngettext("Fixed %d card with invalid properties.",
                         "Fixed %d cards with invalid properties.", cnt) % cnt)
            self.db.execute("update cards set odid=0, odue=0 where id in "+
                ids2str(ids))
        # tags
        self.tags.registerNotes()
        # field cache
        for m in self.models.all():
            self.updateFieldCache(self.models.nids(m))
        # new cards can't have a due position > 32 bits
        self.db.execute("""
update cards set due = 1000000, mod = ?, usn = ? where due > 1000000
and type = 0""", intTime(), self.usn())
        # new card position
        self.conf['nextPos'] = self.db.scalar(
            "select max(due)+1 from cards where type = 0") or 0
        # reviews should have a reasonable due #
        ids = self.db.list(
            "select id from cards where queue = 2 and due > 100000")
        if ids:
            problems.append("Reviews had incorrect due date.")
            self.db.execute(
                "update cards set due = ?, ivl = 1, mod = ?, usn = ? where id in %s"
                % ids2str(ids), self.sched.today, intTime(), self.usn())
        # v2 sched had a bug that could create decimal intervals
        curs = self.db.cursor()

        curs.execute("update cards set ivl=round(ivl),due=round(due) where ivl!=round(ivl) or due!=round(due)")
        if curs.rowcount:
            problems.append("Fixed %d cards with v2 scheduler bug." % curs.rowcount)

        curs.execute("update revlog set ivl=round(ivl),lastIvl=round(lastIvl) where ivl!=round(ivl) or lastIvl!=round(lastIvl)")
        if curs.rowcount:
            problems.append("Fixed %d review history entries with v2 scheduler bug." % curs.rowcount)
        # and finally, optimize
        self.optimize()
        newSize = os.stat(self.path)[stat.ST_SIZE]
        txt = _("Database rebuilt and optimized.")
        ok = not problems
        problems.append(txt)
        # if any problems were found, force a full sync
        if not ok:
            self.modSchema(check=False)
        self.save()
        return ("\n".join(problems), ok)

    def optimize(self):
        self.db.setAutocommit(True)
        self.db.execute("vacuum")
        self.db.execute("analyze")
        self.db.setAutocommit(False)
        self.lock()

    # Logging
    ##########################################################################

    def log(self, *args, **kwargs):
        if not self._debugLog:
            return
        def customRepr(x):
            if isinstance(x, str):
                return x
            return pprint.pformat(x)
        path, num, fn, y = traceback.extract_stack(
            limit=2+kwargs.get("stack", 0))[0]
        buf = "[%s] %s:%s(): %s" % (intTime(), os.path.basename(path), fn,
                                     ", ".join([customRepr(x) for x in args]))
        self._logHnd.write(buf + "\n")
        if devMode:
            print(buf)

    def _openLog(self):
        if not self._debugLog:
            return
        lpath = re.sub(r"\.anki2$", ".log", self.path)
        if os.path.exists(lpath) and os.path.getsize(lpath) > 10*1024*1024:
            lpath2 = lpath + ".old"
            if os.path.exists(lpath2):
                os.unlink(lpath2)
            os.rename(lpath, lpath2)
        self._logHnd = open(lpath, "a", encoding="utf8")

    def _closeLog(self):
        if not self._debugLog:
            return
        self._logHnd.close()
        self._logHnd = None

    # Card Flags
    ##########################################################################

    def setUserFlag(self, flag, cids):
        assert 0 <= flag <= 7
        self.db.execute("update cards set flags = (flags & ~?) | ?, usn=?, mod=? where id in %s" %
                        ids2str(cids), 0b111, flag, self._usn, intTime())
