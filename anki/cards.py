# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import pprint

import time
from anki.hooks import runHook
from anki.utils import intTime, timestampID, joinFields
from anki.consts import *

# Cards
##########################################################################

# Type: 0=new, 1=learning, 2=due
# Queue: same as above, and:
#        -1=suspended, -2=user buried, -3=sched buried
# Due is used differently for different queues.
# - new queue: note id or random int
# - rev queue: integer day
# - lrn queue: integer timestamp
#
# 卡片内的字段说明：
# type:
#   卡片的类型包括：
#       0 = 新卡片，创建后还没有学习过的卡片。
#       1 = 正在学习的卡片。
#       2 = 等待复习的卡片。
#
# queue：
#   卡片所在的队列，除了上面三个同名队列，还有：
#       0=新卡片队列，1=正在学习的卡片队列，2=等待复习的卡片队列
#       3=日学习队列（当前学习步骤的间隔时间超出当天截止时间时，放到此队列中。）
#       -1=休眠的卡片队列，-2=搁置的卡片队列。
#
# due：
#   在不同的队列中有不同的用法
#       - 新卡片队列中：笔记id或随机整数，用于按添加顺序或随机顺序抽取新卡片来学习
#       - 等待复习的卡片队列中：整型日期，下一次复习时的日期。
#       - 学习队列中：整型时间戳
#
# reps：卡片的重复（回答）次数
# did: 卡片的牌组(deck) ID
# inv: 距下一次复习的间隔天数
# factor：排期倍数，
#   一张新卡片学习完成后，每一次进入复习卡片时，值为Options/New cards/Starting ease的值。
#   默认为250%，比如上一次复习间隔为10天，那么下一次的间隔时间就为10*(250%100)=25天。
# odue: old due 卡片加入临时牌组前的复习日期
# odid: old deck id 卡片加入临时牌组前所在的牌组
# mod：卡片最后一次修改的时间戳
# ord：卡片的卡片模板索引。卡片使用笔记类型中的哪一个的卡片模板。
# col: 卡片所属的牌组集。
# timerStarted: 用户开始回答卡片的开始计时。
# id: 卡片ID。
# lapses：遗忘次数计数，用户对卡片回答Again的次数。
# left：卡片当前的所处的学习步骤。低三位为剩余步数，其余高位为从现在到当天截止前可以完成的步数。
# flags: 卡片的标记。
# data: 数据库中保存的内容。

class Card:
    """卡片类"""

    def __init__(self, col, id=None):
        """
        初始化。

        :param col: 卡片所在的牌组集
        :param id: 卡片ID，不为空时从数据库中加载卡片的数据，否则初始化为空卡。
        """

        self.col = col
        self.timerStarted = None
        self._qa = None
        self._note = None
        if id:
            self.id = id
            self.load()
        else:
            # to flush, set nid, ord, and due
            self.id = timestampID(col.db, "cards")
            self.did = 1
            self.crt = intTime()
            self.type = 0
            self.queue = 0
            self.ivl = 0
            self.factor = 0
            self.reps = 0
            self.lapses = 0
            self.left = 0
            self.odue = 0
            self.odid = 0
            self.flags = 0
            self.data = ""

    def load(self):
        """从数据库中加载卡片数据。"""
        (self.id,
         self.nid,
         self.did,
         self.ord,
         self.mod,
         self.usn,
         self.type,
         self.queue,
         self.due,
         self.ivl,
         self.factor,
         self.reps,
         self.lapses,
         self.left,
         self.odue,
         self.odid,
         self.flags,
         self.data) = self.col.db.first(
             "select * from cards where id = ?", self.id)
        self._qa = None
        self._note = None

    def flush(self):
        """卡片数据回存到数据库。"""

        self.mod = intTime()
        self.usn = self.col.usn()
        # bug check
        if self.queue == 2 and self.odue and not self.col.decks.isDyn(self.did):
            runHook("odueInvalid")
        assert self.due < 4294967296
        self.col.db.execute(
            """
insert or replace into cards values
(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            self.id,
            self.nid,
            self.did,
            self.ord,
            self.mod,
            self.usn,
            self.type,
            self.queue,
            self.due,
            self.ivl,
            self.factor,
            self.reps,
            self.lapses,
            self.left,
            self.odue,
            self.odid,
            self.flags,
            self.data)
        self.col.log(self)

    def flushSched(self):
        """ 回存卡片中和调度有关的数据到数据库中"""
        self.mod = intTime()
        self.usn = self.col.usn()
        # bug checks
        if self.queue == 2 and self.odue and not self.col.decks.isDyn(self.did):
            runHook("odueInvalid")
        assert self.due < 4294967296
        self.col.db.execute(
            """update cards set
mod=?, usn=?, type=?, queue=?, due=?, ivl=?, factor=?, reps=?,
lapses=?, left=?, odue=?, odid=?, did=? where id = ?""",
            self.mod, self.usn, self.type, self.queue, self.due, self.ivl,
            self.factor, self.reps, self.lapses,
            self.left, self.odue, self.odid, self.did, self.id)
        self.col.log(self)

    def q(self, reload=False, browser=False):
        """返回卡片的问题的html代码。"""
        return self.css() + self._getQA(reload, browser)['q']

    def a(self):
        """返回卡片的答案的html代码。"""
        return self.css() + self._getQA()['a']

    def css(self):
        """返回卡片的css代码。"""
        return "<style>%s</style>" % self.model()['css']

    def _getQA(self, reload=False, browser=False):
        """
        获取卡片的问题和答案。

        :param reload: True=重新生成数据，
        :param browser: Ture=用于浏览。
        :return: 返回卡片的问题和答案的html代码。
        """
        if not self._qa or reload:
            # 获取笔记、笔记类型、模板。
            f = self.note(reload)
            m = self.model()
            t = self.template()

            # 构造参数：卡片ID、笔记ID、牌组ID、卡片模板索引、笔记的标签、笔记的字段。
            data = [self.id, f.id, m['id'], self.odid or self.did, self.ord,
                    f.stringTags(), f.joinedFields()]
            if browser:
                args = (t.get('bqfmt'), t.get('bafmt'))
            else:
                args = tuple()

            # 返回卡片的问题和答案的html字串。
            self._qa = self.col._renderQA(data, *args)
        return self._qa

    def note(self, reload=False):
        """
        获取卡片所属的笔记。

        :param reload: True=重新加载。
        :return:  返回卡片所属的笔记。
        """
        if not self._note or reload:
            self._note = self.col.getNote(self.nid)
        return self._note

    def model(self):
        """获取卡片所属笔记的笔记类型。"""
        return self.col.models.get(self.note().mid)

    def template(self):
        """
        获取卡片使用的卡片模板。

        笔记类型有两种类型：
            - 正反型MODEL_STD，可以有多个卡片模板。
            - 填空型MODEL_CLOZE，只有一个卡片模板。

        用户在卡片浏览界面通过Cards...按钮可以管理卡片模板。

        :return: 无。
        """
        m = self.model()
        if m['type'] == MODEL_STD:
            # 正反型的笔记类型，可以有多个卡片模板。
            # 所以根据模板索引返回相应的卡片模板。
            return self.model()['tmpls'][self.ord]
        else:
            # 填空型的笔记类型，只能有一个卡片模板。
            # 所以直接返回['tmpls'][0]。
            return self.model()['tmpls'][0]

    def startTimer(self):
        """开始计时。"""
        self.timerStarted = time.time()

    def timeLimit(self):
        """
        获取回答卡片最大的有效时长，以毫秒为单位。

        避免用户有事离开，而导到回答卡片的时间太长。
        在Options/General/Ignore answer time long than xxx seconds中设置。
        默认为60秒，表示回答卡片的时间超过60秒，按60秒计。
        """
        "Time limit for answering in milliseconds."
        conf = self.col.decks.confForDid(self.odid or self.did)
        return conf['maxTaken']*1000

    def shouldShowTimer(self):
        """
        是否在回答时显示计时器。

        在Options/General/Show answer timer中设置。
        """
        conf = self.col.decks.confForDid(self.odid or self.did)
        return conf['timer']

    def timeTaken(self):
        """返回回答卡片耗费的时间，以毫秒为单位。"""

        "Time taken to answer card, in integer MS."
        total = int((time.time() - self.timerStarted)*1000)
        return min(total, self.timeLimit())

    def isEmpty(self):
        #？
        ords = self.col.models.availOrds(
            self.model(), joinFields(self.note().fields))
        if self.ord not in ords:
            return True

    def __repr__(self):
        """返回可打印的对像字符串。"""
        d = dict(self.__dict__)
        # remove non-useful elements
        del d['_note']
        del d['_qa']
        del d['col']
        del d['timerStarted']
        return pprint.pformat(d, width=300)

    def userFlag(self):
        """返回用户标记。"""
        return self.flags & 0b111

    def setUserFlag(self, flag):
        """
        设置用户标记。

        在卡片浏览界面的右键菜单flag中设置。
        :param flag: 标记值。
        :return: 无
        """
        assert 0 <= flag <= 7
        self.flags = (self.flags & ~0b111) | flag
