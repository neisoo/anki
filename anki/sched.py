# -*- coding: utf-8 -*-
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import time
import random
import itertools
from operator import itemgetter
from heapq import *

#from anki.cards import Card
from anki.utils import ids2str, intTime, fmtTimeSpan
from anki.lang import _
from anki.consts import *
from anki.hooks import runHook

# queue types: 0=new/cram, 1=lrn, 2=rev, 3=day lrn, -1=suspended, -2=buried
# revlog types: 0=lrn, 1=rev, 2=relrn, 3=cram
# positive revlog intervals are in days (rev), negative in seconds (lrn)

#
# 卡片有三类类型：0 新卡片：没有学习过的卡片。
#                 1 正在学习卡片：正在执行每个学习步骤的卡片，来源于的新卡片，
#                                  或者复习时回答1表示遗忘的复习卡片。
#                 2 复习卡片：完成所有学习步骤的卡片。到达指定日期后显示给用户复习。
#
# 卡片记录了自己所属的队列，一共有5种：
#  0 新卡片队列：
#   当新增一个卡片时，默认放入这个队列中。队列中卡片都还没有学习过，
#   调度器会从这个队列中抽取一些到期的卡片进行学习。
#
#  1 学习卡片队列：
#   调度器抽取到新卡片后，会把这些卡片移到这队列中。或者复习卡片队列中的卡片在
#   回答Again表示遗忘后，也移到这个队列中重新学习。
#
#  2 复习卡片队列：
#   当学习卡片队列中的卡片完成所有学习步骤后，或者回答Easy直接完成学习后，
#   调度器将这些卡片移到这个队列，并设置初始的到期日。当卡片到期后，调度器从
#   这个队列抽取卡片进行复习。
#
#  3 日学习卡片队列：
#   当天无法完成所在学习步骤，而被安排进明天的学习的卡片。
#
#  -1 休眠卡片队列：
#   这个队列中的卡片不会被调度。
#
#  -2 搁置卡片队列：同一个笔记下的卡片，由于内容相近，所以当其中一张卡片被抽出来学习或复习时，
#                   为了避免互相干扰，通常不希望其它兄弟卡片在同一天的学习或复习中出现。
#                   这时调度器会将兄弟卡片移到这个队列中，到明天再学习或复习。
#
# 调度器内部维护了三种的队列：
#   0 新卡片队列：从活动牌组中抽取出的已经到期的新卡片，作为当天要学习的卡片。
#   1 学习卡片队列：正在学习的卡片。
#   2 复习队列：从活动牌组中抽取出的已经到期的复习卡片，作为当天的复习的卡片。
#
# 调度器抽取的新卡片和复习卡片的数量会根据到牌组和父牌组设置的数量进行限制。
#

class Scheduler:
    name = "std"
    haveCustomStudy = True
    _spreadRev = True
    _burySiblingsOnAnswer = True

    def __init__(self, col):
        """构造排期器，基于一个collection。"""

        # 要调度的牌组集
        self.col = col

        # 调度器内各个队列的最大长度
        self.queueLimit = 50
        self.reportLimit = 1000
        self.reps = 0

        # 今天的日期
        self.today = None

        # 调度器内各个队列是否已经初始化
        self._haveQueues = False
        self._updateCutoff()

    def getCard(self):
        """从队列中取出下一张要回答的卡片，没有卡片时返回空。"""
        "Pop the next card from the queue. None if finished."

        # 跨日时重置调度器。
        self._checkDay()

        # 调度器中队列还没有初始化时重置调度器。
        if not self._haveQueues:
            self.reset()

        # 按一定的优先级取卡。
        card = self._getCard()
        if card:
            self.col.log(card)

            # 搁置兄弟卡片，避免在同一天出现互相干挠。
            if not self._burySiblingsOnAnswer:
                self._burySiblings(card)
            self.reps += 1

            # 卡片开始计时，用于记录回答这张卡片的耗时。
            card.startTimer()

            # 返回卡片。
            return card

    def reset(self):
        """重置调度器"""
        self._updateCutoff()
        self._resetLrn()
        self._resetRev()
        self._resetNew()
        self._haveQueues = True

    def answerCard(self, card, ease):
        """
        回答getCard()取出的卡片

        根据回答，对卡片进行相应的调度。
        :param card:卡片
        :param ease:回答
        :return:
        """

        self.col.log()
        assert 1 <= ease <= 4
        self.col.markReview(card)

        # 隐藏同一笔记下的其它卡片，如果用户有勾选
        # Options/New cards/Bury related new cards until next day。
        if self._burySiblingsOnAnswer:
            self._burySiblings(card)

        # 累加卡片重复次数
        card.reps += 1

        # former is for logging new cards, latter also covers filt. decks
        card.wasNew = card.type == 0
        wasNewQ = card.queue == 0
        if wasNewQ:
            # 新卡片队列中的卡片加入学习卡片队列

            # 卡片之前在新卡片队列，现在将其移动到正在学习卡片队列。
            # came from the new queue, move to learning
            card.queue = 1

            # 如果之前卡片类型是新卡片，现在修改为类型为正在学习的卡片
            # if it was a new card, it's now a learning card
            if card.type == 0:
                card.type = 1

            # 获取学完该卡片的剩余步数（还要重复的次数）
            # init reps to graduation
            card.left = self._startingLeft(card)

            # dynamic?
            # 如果这张卡片还是临时牌组中的复习卡片，并且需要排期。
            # 那么在第一次见到这张复习卡片时就排期。
            if card.odid and card.type == 2:
                if self._resched(card):
                    # reviews get their ivl boosted on first sight
                    card.ivl = self._dynIvlBoost(card)
                    card.odue = self.today + card.ivl

            # 增加卡片所在牌组和其所有父牌组的'新卡片'计数
            self._updateStats(card, 'new')

        if card.queue in (1, 3):
            # 回答学习队列和日学习队列中的卡片
            self._answerLrnCard(card, ease)

            # 增加卡片所在牌组和其所有父牌组的'学习卡片'计数
            if not wasNewQ:
                self._updateStats(card, 'lrn')
        elif card.queue == 2:
            # 回答复习队列中的卡片
            self._answerRevCard(card, ease)

            # 增加卡片所在牌组和其所有父牌组的'复习卡片'计数
            self._updateStats(card, 'rev')
        else:
            raise Exception("Invalid queue")

        # 增加卡片所在牌组和其所有父牌组的'答卡花费的时间'计数
        self._updateStats(card, 'time', card.timeTaken())

        # 更新卡片修改时间
        card.mod = intTime()

        # 牌组集的唯一串号。
        card.usn = self.col.usn()

        # 回存和卡片的调度有关的数据。
        card.flushSched()

    def counts(self, card=None):
        """
        获取新卡片、学习卡片、复习卡片的数量。

        如果card不为空，还要根据card的所在队列，累加进对应的数量中。
        :param card: 卡片
        :return: 返回数量元组
        """

        counts = [self.newCount, self.lrnCount, self.revCount]
        if card:
            idx = self.countIdx(card)
            if idx == 1:
                counts[1] += card.left // 1000
            else:
                counts[idx] += 1
        return tuple(counts)

    def dueForecast(self, days=7):
        """
        预测今后几天要复习的卡片数量，包括今天。

        :param days:预测天数。
        :return: 按日期顺序，返回每一天的要复习的卡片数量数组。
        """
        "Return counts over next DAYS. Includes today."

        # 从cards表中查找指定日期范围内所有到期日的复习卡片的总数，
        # 并按到期日排序和分组。
        daysd = dict(self.col.db.all("""
select due, count() from cards
where did in %s and queue = 2
and due between ? and ?
group by due
order by due""" % self._deckLimit(),
                            self.today,
                            self.today+days-1))

        # 处理日期范围内那些没有复习卡到期的日期。
        for d in range(days):
            d = self.today+d
            if d not in daysd:
                daysd[d] = 0

        # 按日期顺序，返回计数数组。
        # return in sorted order
        ret = [x[1] for x in sorted(daysd.items())]
        return ret

    def countIdx(self, card):
        """根据卡片所在队列，获取卡片的计数索引。"""
        if card.queue == 3:
            return 1
        return card.queue

    def answerButtons(self, card):
        """返回回答一张卡片时要显示的按钮数量。"""

        # 临时牌组
        if card.odue:
            # normal review in dyn deck?
            if card.odid and card.queue == 2:
                # 临时牌组中的复习卡片显示4个按钮 Again,Hard,Good,Easy
                return 4
            conf = self._lrnConf(card)
            if card.type in (0, 1) or len(conf['delays']) > 1:
                # 如果是新卡片或学习卡片或学习步数大于1的
                # 显示3个按钮 Again,Good,Easy
                return 3
            # 其余显示2个按钮 Again,Good
            return 2
        elif card.queue == 2:
            # 复习队列中的卡片显示4个按钮 Again,Hard,Good,Easy
            return 4
        else:
            # 其它队列中的卡片（新卡片、学习卡片、重新学习）显示3个按钮 Again,Good,Easy
            return 3

    def unburyCards(self):
        """
        还原所有被搁置的卡片

        查找cards表中所有在搁置队列中的卡片，将其放回卡片类型对应的队列中。
        """

        "Unbury cards."
        self.col.conf['lastUnburied'] = self.today
        self.col.log(
            self.col.db.list("select id from cards where queue = -2"))
        self.col.db.execute(
            "update cards set queue=type where queue = -2")

    def unburyCardsForDeck(self):
        """取消当前所有活动牌组中所有被搁置的卡片"""
        sids = ids2str(self.col.decks.active())
        self.col.log(
            self.col.db.list("select id from cards where queue = -2 and did in %s"
                             % sids))
        self.col.db.execute(
            "update cards set mod=?,usn=?,queue=type where queue = -2 and did in %s"
            % sids, intTime(), self.col.usn())

    # Rev/lrn/time daily stats
    ##########################################################################

    def _updateStats(self, card, type, cnt=1):
        """
        更新卡片所在牌组和所有父牌组的状态的计数。

        例如用于计录某个牌组当天有多少新卡片，学了多少的卡片，复习了多少卡片。
        :param card:卡数
        :param type:状态索引，有:'new' - 今天新卡片总数
                              'lrn' - 今天学习的卡片总数
                              'rev' - 今天复习的卡片总数
                              'time' - 今天答卡总共花费时间
        :param cnt:增加的状态计数，默认累加1。
        :return:无
        """
        key = type+"Today"
        for g in ([self.col.decks.get(card.did)] +
                  self.col.decks.parents(card.did)):
            # add
            g[key][1] += cnt
            self.col.decks.save(g)

    def extendLimits(self, new, rev):
        """
        修改当前牌组所在树中所有牌组当天新卡片和复习卡片数量。

        :param new: 新卡片调整值
        :param rev: 复习卡片调整值
        :return:无
        """
        cur = self.col.decks.current()
        parents = self.col.decks.parents(cur['id'])
        children = [self.col.decks.get(did) for (name, did) in
                    self.col.decks.children(cur['id'])]
        for g in [cur] + parents + children:
            # add
            g['newToday'][1] -= new
            g['revToday'][1] -= rev
            self.col.decks.save(g)

    def _walkingCount(self, limFn=None, cntFn=None):
        """
        获取活动牌组的某类卡片当天可以抽取的最大数量。

        :param limFn: 回调 - 计算某牌组中当天还可以抽取的某类卡片数量
        :param cntFn: 回调 - 计算某牌组中某类卡片数量
        :return: 返回活动牌组的某类卡片当天可以抽取的最大数量。
        """
        tot = 0
        pcounts = {}

        # 遍历活动牌组。
        # for each of the active decks
        nameMap = self.col.decks.nameMap()
        for did in self.col.decks.active():
            # early alphas were setting the active ids as a str
            did = int(did)

            # 计算这个牌组和这个牌组的所有父牌组中某类卡片的
            # 可抽取的数量限制。然后这些数量限制的最小值，
            # 即为这个牌组应该采用的最小值。也就是：
            # lim = 这个牌组当天最多还可以抽取多少张这种类型的卡片。
            #
            # pcounts[] 中缓存了父牌组的某类卡片的可抽取数量限制。
            # 这个起到优化作用，减少limFn的调用次数。

            # get the individual deck's limit
            lim = limFn(self.col.decks.get(did))
            if not lim:
                continue

            # check the parents
            parents = self.col.decks.parents(did, nameMap)
            for p in parents:
                # add if missing
                if p['id'] not in pcounts:
                    pcounts[p['id']] = limFn(p)
                # take minimum of child and parent
                lim = min(pcounts[p['id']], lim)

            # cnt = 这个牌组实际当天最多可以抽取多少张这个类型的卡片。
            # 因为实际上可能没有那么多牌可供抽取，所以cnt<=lim。

            # see how many cards we actually have
            cnt = cntFn(did, lim)

            # 当这个牌组抽取这么多张类型的卡片后
            # 更新这个牌组和所有父牌组的数量限制。

            # if non-zero, decrement from parent counts
            for p in parents:
                pcounts[p['id']] -= cnt

            # we may also be a parent
            pcounts[did] = lim - cnt

            # 累加抽取数量
            # and add to running total
            tot += cnt

        # 返回所有活动牌组中的某类卡片当天可以抽取的最大数量。
        return tot

    # Deck list
    ##########################################################################

    def deckDueList(self):
        """
        获取线性结构的牌组列表，列表每个元素的结构为：
        [牌组名字，牌组ID，复习卡片数量，学习卡片数量，新卡片数量]

        例如下面的牌组结构
        a::a1::a2
        b::b1
        c

        表示为：
        [
            [a,         1, xx, xx, xx]
            [a::a1,     2, xx, xx, xx]
            [a::a1::a2, 3, xx, xx, xx]
            [b,         4, xx, xx, xx]
            [b::b1,     5, xx, xx, xx]
            [c,         6, xx, xx, xx]
        ]
        """

        "Returns [deckname, did, rev, lrn, new]"
        self._checkDay()
        self.col.decks.checkIntegrity()
        decks = self.col.decks.all()
        decks.sort(key=itemgetter('name'))
        lims = {}
        data = []

        # 获取父牌组名
        def parent(name):
            parts = name.split("::")
            if len(parts) < 2:
                return None
            parts = parts[:-1]
            return "::".join(parts)

        # 遍历牌组。
        for deck in decks:
            p = parent(deck['name'])

            # new = 这个牌组当天可添加的新卡片数
            # new
            nlim = self._deckNewLimitSingle(deck)
            if p:
                nlim = min(nlim, lims[p][0])
            new = self._newForDeck(deck['id'], nlim)

            # lrn = 这个牌组当天要学习的卡片数
            # learning
            lrn = self._lrnForDeck(deck['id'])

            # rlim = 这个牌组当天可添加的复习卡片数
            # reviews
            rlim = self._deckRevLimitSingle(deck)
            if p:
                rlim = min(rlim, lims[p][1])
            rev = self._revForDeck(deck['id'], rlim)

            # save to list
            data.append([deck['name'], deck['id'], rev, lrn, new])

            # add deck as a parent
            lims[deck['name']] = [nlim, rlim]
        return data

    def deckDueTree(self):
        """
        返回树状结构的牌组列表，列表每个元素的结构为：
        [牌组名字，牌组ID，复习卡片数量，学习卡片数量，新卡片数量，[子牌组的树状结构列表]]

        例如下面的牌组结构
        a::a1::a2
        b::b1
        c

        线性结构grps表示为：
        [
            [a,         1, xx, xx, xx]
            [a::a1,     2, xx, xx, xx]
            [a::a1::a2, 3, xx, xx, xx]
            [b,         4, xx, xx, xx]
            [b::b1,     5, xx, xx, xx]
            [c,         6, xx, xx, xx]
        ]

        树型结构表示为：
        [
            [a, 1, xx, xx, xx, [a1, 2, xx, xx, xx, [a2, 3, xx, xx, xx]]],
            [b, 4, xx, xx, xx, [b1, 5, xx, xx, xx]],
            [c, 6, xx, xx, xx]
        ]


        :param grps:线性结构表示的牌组列表。
        :return:树型结构表示的牌组列表。
        """

        """返回树状结构的牌组列表，包括到牌组期信息"""
        return self._groupChildren(self.deckDueList())

    def _groupChildren(self, grps):
        """将树状结构的牌组列表转换成树型结构列表"""
        # first, split the group names into components
        for g in grps:
            g[0] = g[0].split("::")
        # and sort based on those components
        grps.sort(key=itemgetter(0))
        # then run main function
        return self._groupChildrenMain(grps)

    def _groupChildrenMain(self, grps):
        """
        将树状结构的牌组列表转换成树型结构列表的递归实现

        :param grps:线性结构表示的牌组列表。
        :return:树型结构表示的牌组列表。
        """

        tree = []
        # group and recurse
        def key(grp):
            return grp[0][0]

        # 按顶层牌组名分组并按分组遍历
        for (head, tail) in itertools.groupby(grps, key=key):
            tail = list(tail)
            did = None
            rev = 0
            new = 0
            lrn = 0
            children = []
            # 遍历该顶层牌组名下的子牌组
            for c in tail:
                if len(c[0]) == 1:
                    # 叶子牌组
                    # current node
                    did = c[1]
                    rev += c[2]
                    lrn += c[3]
                    new += c[4]
                else:
                    # 非叶子牌组，添加到children列表，后面递归用。
                    # set new string to tail
                    c[0] = c[0][1:]
                    children.append(c)
            # 用同样的方法递归所有的子牌组。
            children = self._groupChildrenMain(children)
            # 累计各个子牌组的各类卡片数做为自己的卡片数。
            # tally up children counts
            for ch in children:
                rev += ch[2]
                lrn += ch[3]
                new += ch[4]
            # 按牌组中设置的限制来限制各类卡片数。
            # limit the counts to the deck's limits
            conf = self.col.decks.confForDid(did)
            deck = self.col.decks.get(did)
            if not conf['dyn']:
                rev = max(0, min(rev, conf['rev']['perDay']-deck['revToday'][1]))
                new = max(0, min(new, conf['new']['perDay']-deck['newToday'][1]))
            tree.append((head, did, rev, lrn, new, children))

        # 转成元组类型，元组类型是只读的。
        return tuple(tree)

    # Getting the next card
    ##########################################################################

    def _getCard(self):
        """返回下一张到期的卡片，如果没有返回空。"""

        "Return the next due card id, or None."

        # 先取调度器学习队列中的卡片。
        # learning card due?
        c = self._getLrnCard()
        if c:
            return c

        # 适当的时候取调度器新卡片队列中的卡片。
        # new first, or time for one?
        if self._timeForNewCard():
            c = self._getNewCard()
            if c:
                return c

        # 取调度器复习卡片队列中的卡片。
        # card due for review?
        c = self._getRevCard()
        if c:
            return c

        # 取调度器跨日学习卡片队列中的卡片。
        # day learning card due?
        c = self._getLrnDayCard()
        if c:
            return c

        # 取调度器新卡片队列中的卡片。
        # new cards left?
        c = self._getNewCard()
        if c:
            return c

        # 没有其它任何卡片时，提前从学习队列中取出
        # 一张将在未来一段在时间内会到期的卡片。这段时间的长短在
        # Preference/Basic/Learn ahead limit中设置。
        # collapse or finish
        return self._getLrnCard(collapse=True)

    # New cards
    ##########################################################################

    def _resetNewCount(self):
        """重新计算活动牌组当天可以增加新卡片的最大数量。"""

        # 匿名函数,获取cards表中属于指定牌组的并在新卡片队列中的卡片总数
        cntFn = lambda did, lim: self.col.db.scalar("""
select count() from (select 1 from cards where
did = ? and queue = 0 limit ?)""", did, lim)

        self.newCount = self._walkingCount(self._deckNewLimitSingle, cntFn)

    def _resetNew(self):
        """重置调度器中新卡片相关数据"""
        self._resetNewCount()
        self._newDids = self.col.decks.active()[:]
        self._newQueue = []
        self._updateNewCardRatio()

    def _fillNew(self):
        """
        填充调度器的新卡片队列。

        :return: 返回Ture表示调度器的新卡片队列不为空，否则表示没有当天没有更多的新卡片需要学习
        """

        # 调度器的新卡片队列还有卡片，返回ture。
        if self._newQueue:
            return True

        # 已经达到当日新卡片数量的限制，返回false。
        if not self.newCount:
            return False

        # 调度器的新卡片队列为空，但还没有抽取足够的新卡片（没有达到当日学习新卡片的限制）时，
        # 那么就从活动牌组中找出一个有新卡片的牌组，并从它的新卡片队列中抽取新卡片，填充到调度器的新卡片队列中。

        # 遍历可提供新卡片的牌组
        while self._newDids:
            # lim:获得这个牌组当日可添加到新卡片队列中的新卡片数量限制
            did = self._newDids[0]
            lim = min(self.queueLimit, self._deckNewLimit(did))
            if lim:
                # 从这个牌组的新卡片队列中抽取lim新张卡片到内存中的新卡片队列中。
                # 因为新卡片队列中的due值为笔记id或随机整数，
                # 所以按order排序就可以实现按添加顺序或随机顺序抽取新卡片来学习。
                # fill the queue with the current did

                # 反序并返回卡ID列表。
                self._newQueue = self.col.db.list("""
                select id from cards where did = ? and queue = 0 order by due,ord limit ?""", did, lim)
                if self._newQueue:
                    self._newQueue.reverse()
                    return True

            # 这个牌组中没有新卡片，查看下一个牌组。
            # nothing left in the deck; move to next
            self._newDids.pop(0)

        # 全部都没有新卡片，但可添加新卡片数量又不为零。
        # 那么重置新卡片数据，再重新检查，直到符合。
        if self.newCount:
            # if we didn't get a card but the count is non-zero,
            # we need to check again for any cards that were
            # removed from the queue but not buried
            self._resetNew()
            return self._fillNew()

    def _getNewCard(self):
        """从调度器的新卡片队列的队尾取一张卡片。"""

        # 调度器的新卡片队列如果为空，那么继续从活动牌组中抽取新卡片来填充调度器的新卡片队列。
        # 直到返回false，表示没有可抽取的新卡片或达到当日可学习新卡片的限制。
        if self._fillNew():
            # 从调度器的新卡片队列的队尾取一张卡片，并减少计数，当计算减到0时表示
            # 已经达到当日学习新卡片的限制。
            self.newCount -= 1
            return self.col.getCard(self._newQueue.pop())

    def _updateNewCardRatio(self):
        """根据新卡与复习卡片的混合方式设置，来确定每间隔多少张卡片可插入一张新卡片。"""

        # Preference/Basic/Mix new cards and reviews.
        if self.col.conf['newSpread'] == NEW_CARDS_DISTRIBUTE:
            if self.newCount:
                # 每间隔多少张卡片可插入一张新卡片。
                self.newCardModulus = (
                    (self.newCount + self.revCount) // self.newCount)
                # if there are cards to review, ensure modulo >= 2
                if self.revCount:
                    self.newCardModulus = max(2, self.newCardModulus)
                return
        self.newCardModulus = 0

    def _timeForNewCard(self):
        """
        这一次是否应该轮到新卡片显示

        根据Preferences/Basic中新卡的显示顺序设置决定是否该新卡显示了。
        """

        "True if it's time to display a new card when distributing."
        if not self.newCount:
            return False
        if self.col.conf['newSpread'] == NEW_CARDS_LAST:
            return False
        elif self.col.conf['newSpread'] == NEW_CARDS_FIRST:
            return True
        elif self.newCardModulus:
            return self.reps and self.reps % self.newCardModulus == 0

    def _deckNewLimit(self, did, fn=None):
        """计算did牌组当天最多可以抽取多少张某类型卡片到调度器队列中，要考虑父牌组的抽取数量限制。"""

        if not fn:
            fn = self._deckNewLimitSingle
        sel = self.col.decks.get(did)
        lim = -1

        # 遍历牌组和所有父牌组，找出最小限制数量。

        # for the deck and each of its parents
        for g in [sel] + self.col.decks.parents(did):
            rem = fn(g)
            if lim == -1:
                lim = rem
            else:
                lim = min(rem, lim)
        return lim

    def _newForDeck(self, did, lim):
        """
        获到指定牌组中的新卡总数。

        :param did: 牌组ID。
        :param lim: 最大限制。
        :return:返回新卡总数，值不超过lim。
        """
        "New count for a single deck."
        if not lim:
            return 0
        lim = min(lim, self.reportLimit)
        return self.col.db.scalar("""
select count() from
(select 1 from cards where did = ? and queue = 0 limit ?)""", did, lim)

    def _deckNewLimitSingle(self, g):
        """计算牌组中当天最多可以添加多少张新卡片到新卡片队列中，不考虑父牌组的每日新卡片数量限制。"""
        "Limit for deck without parent limits."
        if g['dyn']:
            return self.reportLimit
        c = self.col.decks.confForDid(g['id'])

        # Optins/New cards/"New cards/day" 中的设置 - 当天已有新卡片计数。
        return max(0, c['new']['perDay'] - g['newToday'][1])

    def totalNewForCurrentDeck(self):
        """返回活动牌组中所有新卡片的总数。"""
        return self.col.db.scalar(
            """
select count() from cards where id in (
select id from cards where did in %s and queue = 0 limit ?)"""
            % ids2str(self.col.decks.active()), self.reportLimit)

    # Learning queues
    ##########################################################################

    def _resetLrnCount(self):
        """"重新计算活动牌组当天可以抽取学习卡片的最大数量。"""
        # sub-day
        self.lrnCount = self.col.db.scalar("""
select sum(left/1000) from (select left from cards where
did in %s and queue = 1 and due < ? limit %d)""" % (
            self._deckLimit(), self.reportLimit),
            self.dayCutoff) or 0
        # day
        self.lrnCount += self.col.db.scalar("""
select count() from cards where did in %s and queue = 3
and due <= ? limit %d""" % (self._deckLimit(), self.reportLimit),
                                            self.today)

    def _resetLrn(self):
        """重置调度器中关于学习队列的数据"""
        self._resetLrnCount()
        self._lrnQueue = []
        self._lrnDayQueue = []
        self._lrnDids = self.col.decks.active()[:]

    # sub-day learning
    def _fillLrn(self):
        """填充调度器中的学习队列"""

        # 已经达到当日学习卡片的限制，返回false。
        if not self.lrnCount:
            return False

        # 调度器中的学习队列中还有卡片，不需要填充，返回true。
        if self._lrnQueue:
            return True

        # 从cards数据表中查找活动牌组中的学习队列，并且今天截止前到期的卡片。
        self._lrnQueue = self.col.db.all("""
select due, id from cards where
did in %s and queue = 1 and due < :lim
limit %d""" % (self._deckLimit(), self.reportLimit), lim=self.dayCutoff)

        # 按到期时间排序。
        # as it arrives sorted by did first, we need to sort it
        self._lrnQueue.sort()
        return self._lrnQueue

    def _getLrnCard(self, collapse=False):
        """
        如果调度器的学习队列的队头卡片已经到期，那么从队头取出并返回这张卡片。

        :param collapse:是否考虑提前取卡，见Preference/Basic/Learn ahead limit设置。
        :return:返回的卡片
        """
        """"""
        if self._fillLrn():
            # 截止时间
            cutoff = time.time()
            if collapse:
                cutoff += self.col.conf['collapseTime']
            if self._lrnQueue[0][0] < cutoff:
                id = heappop(self._lrnQueue)[1]
                card = self.col.getCard(id)
                self.lrnCount -= card.left // 1000
                return card

    # daily learning
    def _fillLrnDay(self):
        """填充调度器中的跨日学习队列"""

        # 已经达到当日学习卡片的限制，返回false。
        if not self.lrnCount:
            return False

        # 调度器中的跨日学习队列中还有卡片，不需要填充，返回true。
        if self._lrnDayQueue:
            return True

        # 遍历活动牌组。
        while self._lrnDids:

            # 从数据表cards中查找这个牌组的在日学习队列中的并且到期的卡片。
            did = self._lrnDids[0]
            # fill the queue with the current did
            self._lrnDayQueue = self.col.db.list("""
select id from cards where
did = ? and queue = 3 and due <= ? limit ?""",
                                    did, self.today, self.queueLimit)
            # 抽到卡片了。
            if self._lrnDayQueue:
                # 打乱卡片的顺序。
                # order
                r = random.Random()
                r.seed(self.today)
                r.shuffle(self._lrnDayQueue)

                # 这个牌组没有更多的日学习卡片可抽取，查看下一个牌组。
                # is the current did empty?
                if len(self._lrnDayQueue) < self.queueLimit:
                    self._lrnDids.pop(0)
                return True

            # 这个牌组没有更多的日学习卡片可抽取，查看下一个牌组。
            # nothing left in the deck; move to next
            self._lrnDids.pop(0)

    def _getLrnDayCard(self):
        """从调度器的日学习卡片队列的队头取出一张卡片。"""
        if self._fillLrnDay():
            self.lrnCount -= 1
            return self.col.getCard(self._lrnDayQueue.pop())

    def _answerLrnCard(self, card, ease):
        """
        回答正在学习的卡。（在牌组或临时牌组中的新卡和遗忘的复习卡，）·

        :param card:卡片
        :param ease: 回答的效果，1=Again、2=Good、3=Easy
        :return:无
        """
        # ease 1=no, 2=yes, 3=remove
        conf = self._lrnConf(card)
        if card.odid and not card.wasNew:
            type = 3
        elif card.type == 2:
            type = 2
        else:
            type = 0
        leaving = False
        # lrnCount was decremented once when card was fetched
        lastLeft = card.left

        # 用户回答很容易，那么立即完成学习，直接放到复习卡队列中并排期。
        # immediate graduate?
        if ease == 3:
            self._rescheduleAsRev(card, conf, True)
            leaving = True

        # 用户回答Good，并且完成了所有的步数，那么也表示完成的学习，
        # 加入复习卡队列中并排期。
        # graduation time?
        elif ease == 2 and (card.left%1000)-1 <= 0:
            self._rescheduleAsRev(card, conf, False)
            leaving = True
        else:
            # 用户回答Good，但没有完成所有的步数，则前进到下一步。
            # one step towards graduation
            if ease == 2:
                # 更新剩余步数。
                # 低三位为剩余步数，其余高位为从现在到当天截止前可以完成的步数，
                # decrement real left count and recalculate left today
                left = (card.left % 1000) - 1
                card.left = self._leftToday(conf['delays'], left)*1000 + left
            # 用户在学习卡片时回答Again，表示要回到第一步，重新学习。
            # failed
            else:
                # 初始化步数
                card.left = self._startingLeft(card)

                # 如果卡片需要重排期并且Options/Lapses/New interval设置不为空
                resched = self._resched(card)
                if 'mult' in conf and resched:
                    # 排期，也就是计算新的复习间隔天数。
                    # review that's lapsed
                    card.ivl = max(1, conf['minInt'], card.ivl*conf['mult'])
                else:
                    # 新卡片在学习过程中回答Again时，不需要排期。
                    # new card; no ivl adjustment
                    pass

                # 需要排期并且是临时卡片组，旧排期改为明天。
                if resched and card.odid:
                    card.odue = self.today + 1

            # 获取当前步的时长。
            delay = self._delayForGrade(conf, card.left)

            # 如果回答卡片逾期，为时长加上一些随机性延长。
            if card.due < time.time():
                # not collapsed; add some randomness
                delay *= random.uniform(1, 1.25)

            # 设定新的到期时间戳
            card.due = int(time.time() + delay)

            # due today?
            if card.due < self.dayCutoff:
                # 到期时间没超过今天的截止范围

                # 累加学习计数
                self.lrnCount += card.left // 1000

                # if the queue is not empty and there's nothing else to do, make
                # sure we don't put it at the head of the queue and end up showing
                # it twice in a row

                # 放到学习队列中
                card.queue = 1

                # 如果只剩下学习队列中还有卡片（学习队列不为空，并且复习队列和新卡片队列都为空）
                # 那么这张卡片不要放在学习队列的头部，避免连续出现同一张卡片。
                if self.lrnQuee and not self.revCount and not self.newCount:
                    smallestDue = self._lrnQueue[0][0]
                    card.due = max(card.due, smallestDue+1)
                heappush(self._lrnQueue, (card.due, card.id))
            else:
                # 如果到期时间超出了当天截止时间，那么放到日学习队列中，并设定到期日。
                # the card is due in one or more days, so we need to use the
                # day learn queue
                ahead = ((card.due - self.dayCutoff) // 86400) + 1
                card.due = self.today + ahead
                card.queue = 3

        # 添加log。
        self._logLrn(card, ease, conf, leaving, type, lastLeft)

    def _delayForGrade(self, conf, left):
        """
        返回某一学习步骤的间隔时长，以秒为单位。

        :param conf: 配置，需要其中的Delays设置。
        :param left: 剩余步数。
        :return: 返回当前步骤的时间隔。
        """
        """"""

        left = left % 1000
        try:
            delay = conf['delays'][-left]
        except IndexError:
            if conf['delays']:
                delay = conf['delays'][0]
            else:
                # user deleted final step; use dummy value
                delay = 1
        return delay*60

    def _lrnConf(self, card):
        """获取正在学习的卡片的设置"""
        if card.type == 2:
            # 卡片类型为复习卡，取遗忘设置。
            return self._lapseConf(card)
        else:
            # 卡片类型为新卡片，取新卡片设置。
            return self._newConf(card)

    def _rescheduleAsRev(self, card, conf, early):
        """
        将卡片完成学习后，放入复习卡片队列中，安排复习日期（排期）。

        学习完成的方式分二种：- 学习过程中直接回答Easy，提前毕业，这时early=True。
                            - 学习过程中连续回答Good，完成所有步骤间隔而毕业，这时early=False。

        学习队列中的卡片分二种：- 新卡片，也就是之前没有学习过的卡片，被加入到学习队列中进行学习。
                                - 复习卡片，在复习时回符Again，被当成遗忘的卡片（lapse），
                                  被加入到学习队列中，重新学习。

        :param card:卡片
        :param conf:配置
        :param early:学习完成的方式
        :return:无
        """
        lapse = card.type == 2
        if lapse:
            # 这是一张复习卡片，表示是卡片在复习时因回答Again而被当成遗忘的卡片（lapse）。
            # 对于这样的卡片会加入到学习队列中重新学习，学习的间隔使用Lapses/Setups中的设置。
            # 学习完成后将复习日期按排到明天。
            # 如果该设置为空的话则卡片不会加入到学习队列中，而是用New interval中设置的百分比去减少复习间隔。
            # 这种情况下，不会运行到这里。
            if self._resched(card):
                # 排期到明天复习
                card.due = max(self.today+1, card.odue)
            else:
                # 不需要重排，那么还是使用原来的排期
                card.due = card.odue
            card.odue = 0
        else:
            # 为一张完成学习的新卡，第一次安日排复习日期和排期因子。
            self._rescheduleNew(card, conf, early)

        # 卡片放到等待复习队列中，卡片类型为等待复习卡片。
        card.queue = 2
        card.type = 2

        # 如果是临时牌组中的卡处，完成学习后，要将卡片发还到原来的牌组中。
        # if we were dynamic, graduating means moving back to the old deck
        resched = self._resched(card)
        if card.odid:
            card.did = card.odid
            card.odue = 0
            card.odid = 0

            # 如果这张卡片所在的临时牌组被设置成不需要重新排期
            # 并且不是被遗忘的复习卡片（也就意味着这张学习完成的卡片是新卡片），
            # 那么这张卡片被设置回新卡片。
            # if rescheduling is off, it needs to be set back to a new card
            if not resched and not lapse:
                card.queue = card.type = 0
                card.due = self.col.nextID("pos")

    def _startingLeft(self, card):
        """
        获取初始剩余步数。

        :param card: 卡片
        :return: 低三位为剩余步数，其余高位为从现在到当天截止前可以完成的步数，
        """

        # 根据卡片类型获取steps的设置。
        # - 新卡片类型：使用options/New cards中的steps的设置
        # - 复习卡片类型：使用options/Lapses的中的steps的设置
        if card.type == 2:
            conf = self._lapseConf(card)
        else:
            conf = self._lrnConf(card)
        tot = len(conf['delays'])
        tod = self._leftToday(conf['delays'], tot)
        return tot + tod*1000

    def _leftToday(self, delays, left, now=None):
        """
        获得从当天某个时间点开始到当天截止时间前可以完成的步数。
        :param delays: 每一步的时间间隔数组，以分钟为单位。
        :param left: 剩余步数。
        :param now: 开始计算的时间点，默认为现在。
        :return:返回步数。
        """

        "The number of steps that can be completed by the day cutoff."
        if not now:
            now = intTime()
        delays = delays[-left:]
        ok = 0
        for i in range(len(delays)):
            now += delays[i]*60
            if now > self.dayCutoff:
                break
            ok = i
        return ok+1

    def _graduatingIvl(self, card, conf, early, adj=True):
        """返回卡片完成学习后，首次复习的间隔天数"""

        # 卡片是被遗忘的（lapsed）而重新学习的复习卡。
        if card.type == 2:
            # lapsed card being relearnt
            if card.odid:
                # 卡片所在临时牌组中是否有设置需要根据答复重排期。
                if conf['resched']:
                    # 重新确定间隔天数
                    return self._dynIvlBoost(card)
            # 间隔天数保持不变
            return card.ivl

        # 以下是针对完成学习的新卡片
        # 提前完成学习的，开始复习间隔为Options/New cards/Graduate interval中的值，默认为1天，也就是明天。
        # 完成所有学习步骤的，开始复习间隔为Options/New cards/Easy interval中的值，默认为4天
        # ints为intervals的缩写。
        if not early:
            # graduate
            ideal = conf['ints'][0]
        else:
            # early remove
            ideal = conf['ints'][1]

        # 调整间隔
        if adj:
            return self._adjRevIvl(card, ideal)
        else:
            return ideal

    def _rescheduleNew(self, card, conf, early):
        """为完成学习后的新卡片安排第一次复习日期和排期因子"""

        "Reschedule a new card that's graduated for the first time."
        card.ivl = self._graduatingIvl(card, conf, early)
        card.due = self.today+card.ivl

        # Options/New cards/Starting ease的设置，
        # 初始的间隔天数放大因子。
        card.factor = conf['initialFactor']

    def _logLrn(self, card, ease, conf, leaving, type, lastLeft):
        """新增一条日志到reglog表。"""

        lastIvl = -(self._delayForGrade(conf, lastLeft))
        ivl = card.ivl if leaving else -(self._delayForGrade(conf, card.left))
        def log():
            self.col.db.execute(
                "insert into revlog values (?,?,?,?,?,?,?,?,?)",
                int(time.time()*1000), card.id, self.col.usn(), ease,
                ivl, lastIvl, card.factor, card.timeTaken(), type)
        try:
            log()
        except:
            # duplicate pk; retry in 10ms
            time.sleep(0.01)
            log()

    def removeLrn(self, ids=None):
        """
        将正在学习的卡片发还到它们原来的队列中。

        :param ids:要发还的卡片ID，如果为空表示所有正在学习的卡片。
        :return:无
        """

        "Remove cards from the learning queues."

        if ids:
            extra = " and id in "+ids2str(ids)
        else:
            # benchmarks indicate it's about 10x faster to search all decks
            # with the index than scan the table
            extra = " and did in "+ids2str(self.col.decks.allIds())

        # 将所有在学习队列和日学习队列中的复习卡片移回
        # 复习队列，到期日还原为移动到学习队列前的到期日，
        # 更新修改日期。

        # review cards in relearning
        self.col.db.execute("""
update cards set
due = odue, queue = 2, mod = %d, usn = %d, odue = 0
where queue in (1,3) and type = 2
%s
""" % (intTime(), self.col.usn(), extra))

        # 到这里，还在学习队列和日学习队列中卡片就只有新卡片了。
        # 将新卡片发还新卡片队列中。

        # new cards in learning
        self.forgetCards(self.col.db.list(
            "select id from cards where queue in (1,3) %s" % extra))

    def _lrnForDeck(self, did):
        """计算当天要学习的卡片数量。"""

        # collapseTime是Preferences/Basic/Learn ahead limit的设置。
        # 也就是现在当前牌组中所有学习步骤都学完了，但还有正在学习的卡只是没到期时。
        # 如果collapseTime时间之内会有正在学习的卡到期，并且没有其它的
        # 事要做，那么就会提前学习这张卡，否则显示congratulation表示都完成了。

        # 计算还有多少张卡片需要学习（同一张卡，要学习三个步骤，算3张卡。）
        # 从cards数据表中查找指定牌组的，学习队列中，在未来collapseTime时间之内
        # 到期所有步片学习步数的总数。
        cnt = self.col.db.scalar(
            """
select sum(left/1000) from
(select left from cards where did = ? and queue = 1 and due < ? limit ?)""",
            did, intTime() + self.col.conf['collapseTime'], self.reportLimit) or 0

        # 再加上跨天的日学习队列中的卡片
        return cnt + self.col.db.scalar(
            """
select count() from
(select 1 from cards where did = ? and queue = 3
and due <= ? limit ?)""",
            did, self.today, self.reportLimit)

    # Reviews
    ##########################################################################

    def _deckRevLimit(self, did):
        """返回某个牌组当天最大可抽取的复习卡片数，考虑父牌组的限制"""
        return self._deckNewLimit(did, self._deckRevLimitSingle)

    def _deckRevLimitSingle(self, d):
        """返回某个牌组当天最大可抽取的复习卡片数，不考虑父牌组的限制"""
        if d['dyn']:
            return self.reportLimit
        c = self.col.decks.confForDid(d['id'])
        return max(0, c['rev']['perDay'] - d['revToday'][1])

    def _revForDeck(self, did, lim):
        """返回did牌组当天到期的复习卡片数量，最大值不超过lim。"""
        lim = min(lim, self.reportLimit)
        return self.col.db.scalar(
            """
select count() from
(select 1 from cards where did = ? and queue = 2
and due <= ? limit ?)""",
            did, self.today, lim)

    def _resetRevCount(self):
        """"重新计算活动牌组当天可以抽取复习卡片的最大数量。"""
        def cntFn(did, lim):
            return self.col.db.scalar("""
select count() from (select id from cards where
did = ? and queue = 2 and due <= ? limit %d)""" % lim,
                                      did, self.today)
        self.revCount = self._walkingCount(
            self._deckRevLimitSingle, cntFn)

    def _resetRev(self):
        """重置调度器中关于复习卡片的数据"""
        self._resetRevCount()
        self._revQueue = []
        self._revDids = self.col.decks.active()[:]

    def _fillRev(self):
        """填充调度器的复习卡片队列。"""

        # 调度器的复习卡片队列不为空，返回TRUE。
        if self._revQueue:
            return True

        # 已经达到当日复习卡片数限制，返回FALSE。
        if not self.revCount:
            return False

        # 调度器的复习卡片队列为空，但还没有抽取足够的复习卡片（没有达到当日学习复习卡片的限制）时，
        # 那么就从活动牌组中找出一个有复习卡片的牌组，并从它的复习卡片队列中抽取复习卡片，
        # 填充到调度器的复习卡片队列中。

        # 遍历所有可以提供复习卡片的牌组。
        while self._revDids:
            # lim:获得这个牌组当日可添加到新卡片队列中的新卡片数量限制
            did = self._revDids[0]
            lim = min(self.queueLimit, self._deckRevLimit(did))
            if lim:
                # 从这个牌组的复习卡片队列中抽取lim张卡片到调度器的复习卡片队列。
                # fill the queue with the current did
                self._revQueue = self.col.db.list("""
select id from cards where
did = ? and queue = 2 and due <= ? limit ?""",
                                                  did, self.today, lim)
                if self._revQueue:
                    # ordering
                    if self.col.decks.get(did)['dyn']:
                        # 动态牌组中的到期时间是反的。
                        # dynamic decks need due order preserved
                        self._revQueue.reverse()
                    else:
                        # 随机打乱队列中牌的顺序。
                        # random order for regular reviews
                        r = random.Random()
                        r.seed(self.today)
                        r.shuffle(self._revQueue)

                    # 这个牌组已经取完了所有复习卡片。
                    # 下一次填充时，要从下一个牌组中取卡片。
                    # is the current did empty?
                    if len(self._revQueue) < lim:
                        self._revDids.pop(0)
                    return True

            # 这个牌组中没有可取的复习卡片，查看下一个牌组。
            # nothing left in the deck; move to next
            self._revDids.pop(0)

        # 没有取到复习卡片，但可抽取的复习卡片数量又不为零。
        # 那么重置复习卡片数据，再重新抽取，直到符合。
        if self.revCount:
            # if we didn't get a card but the count is non-zero,
            # we need to check again for any cards that were
            # removed from the queue but not buried
            self._resetRev()
            return self._fillRev()

    def _getRevCard(self):
        """取调度器复习卡片队列中的卡片"""

        if self._fillRev():
            self.revCount -= 1
            return self.col.getCard(self._revQueue.pop())

    def totalRevForCurrentDeck(self):
        """计算活动牌组中共有多少复习队列中的卡片到期，最大不超过动态牌组中设置的是大卡片数。"""
        return self.col.db.scalar(
            """
select count() from cards where id in (
select id from cards where did in %s and queue = 2 and due <= ? limit ?)"""
            % ids2str(self.col.decks.active()), self.today, self.reportLimit)

    # Answering a review card
    ##########################################################################

    def _answerRevCard(self, card, ease):
        """回答一张复习卡片"""

        delay = 0
        if ease == 1:
            # 处理用户回答Again表示遗忘的情况。
            delay = self._rescheduleLapse(card)
        else:
            # 处理其它3种回答的情况。
            self._rescheduleRev(card, ease)
        self._logRev(card, ease, delay)

    def _rescheduleLapse(self, card):
        """
        给遗忘的复习卡片排期。

        回答一张复习卡片时，选择了Again，表示已经遗忘了。
        """

        conf = self._lapseConf(card)
        card.lastIvl = card.ivl

        # 如果需要重排期
        if self._resched(card):
            # 增加遗忘次数
            card.lapses += 1

            # 为遗忘的复习卡片排期：
            # 因子减少0.2并减少间隔天数，减少比例为
            # Options/Lapses/New Interval，默认为0%，也就是明天。
            # 因子不小于1.3
            card.ivl = self._nextLapseIvl(card, conf)
            card.factor = max(1300, card.factor-200)
            card.due = self.today + card.ivl
            # if it's a filtered deck, update odue as well
            if card.odid:
                card.odue = card.due

        # 如果这张卡片被当成难点移到了休眠队列中，则不用做任何事，返回。
        # if spended as a leech, nothing to do
        delay = 0
        if self._checkLeech(card, conf) and card.queue == -1:
            return delay

        # 如果Options/Lapses/Steps为空，无法移动到学习队列中，返回。
        # if no relearning steps, nothing to do
        if not conf['delays']:
            return delay

        # 为后面保存due的日期
        # record rev due date for later
        if not card.odue:
            card.odue = card.due

        # 得到第一步的间隔，排期，初始化剩余步骤。
        delay = self._delayForGrade(conf, 0)
        card.due = int(delay + time.time())
        card.left = self._startingLeft(card)

        # queue 1
        if card.due < self.dayCutoff:
            # 当天能重新学习完所有步骤，那么加入学习队列中。
            self.lrnCount += card.left // 1000
            card.queue = 1
            heappush(self._lrnQueue, (card.due, card.id))
        else:
            # 当天无法完成所有步骤，那么排期并加入到日学习队列中。
            # day learn queue
            ahead = ((card.due - self.dayCutoff) // 86400) + 1
            card.due = self.today + ahead
            card.queue = 3

        # 如果当前要学习，那么返回第一步的延时，否则返回0.
        return delay

    def _nextLapseIvl(self, card, conf):
        """每次回答遗忘时，按比例减少复习间隔天数。"""
        return max(conf['minInt'], int(card.ivl*conf['mult']))

    def _rescheduleRev(self, card, ease):
        """
        给非遗忘的复习卡片排期。

        回答一张复习卡片时，选择了除Again以外的回答。
        :param card:卡片
        :param ease:回答
        :return:
        """
        # update interval
        card.lastIvl = card.ivl
        if self._resched(card):
            # 根据回答更新间隔天数
            self._updateRevIvl(card, ease)

            # 调整因子：
            # Easy: 因子增加0.15
            # Good: 因子不变
            # Hard: 因子减少0.15
            # 因子不小于1.3

            # then the rest
            card.factor = max(1300, card.factor+[-150, 0, 150][ease-2])

            # 更新到期日
            card.due = self.today + card.ivl
        else:
            # 不用排期，使用之前保存的值
            card.due = card.odue

        # 如果是临时牌组中的，将卡片发还到原牌组。
        if card.odid:
            card.did = card.odid
            card.odid = 0
            card.odue = 0

    def _logRev(self, card, ease, delay):
        """添加一条复习日志。"""

        def log():
            self.col.db.execute(
                "insert into revlog values (?,?,?,?,?,?,?,?,?)",
                int(time.time()*1000), card.id, self.col.usn(), ease,
                -delay or card.ivl, card.lastIvl, card.factor, card.timeTaken(),
                1)
        try:
            log()
        except:
            # duplicate pk; retry in 10ms
            time.sleep(0.01)
            log()

    # Interval management
    ##########################################################################

    def _nextRevIvl(self, card, ease):
        """
        根据回答，确定卡片的下一次复习间隔。

        算法：
          ease = 2，表示Hard。新的间隔 = (当前间隔天数 + 迟到天数 // 4) * 1.2
          ease = 3，表示Good。新的间隔 = (当前间隔天数 + 迟到天数 // 2） * 因子
          ease = 4，表示Easy。新的间隔 = (当前间隔天数 + 迟到天数） * 因子 * Easy bonus

          回答Hard时，表示有点困难，间隔天数会是之前的1.2倍，间隔天数增加比较慢。
          回答Good时，表示记忆的比较好，就用现有的因子计算新的间隔天数，因子的初始值为2.5，
          在Options/New cards/Starting ease中设置。
          回答Easy时，表示太简单，就会在现有因子的基础上再乘以一个倍数，这个倍数
          在Options/Reviews/Easy bonus中设置，这样可以快速放大间隔天数。

          新的间隔天数最后还可以乘以一个倍数做为最终的值，这个倍数在
          Options/Review/interval modifier中设置。

          因子的初始值为2.5，每次回答Again时会减少0.2且不大于1.3。
            # 调整因子：
            # Again: 因子减少0.2并减少间隔天数，减少比例为Options/Lapses/New Interval，默认为0%，也就是明天。
            # Hard: 因子减少0.15
            # Good: 因子不变
            # Easy: 因子增加0.15

        :param card: 卡片
        :param ease: 回答
        :return:
        """
        "Ideal next interval for CARD, given EASE."
        delay = self._daysLate(card)
        conf = self._revConf(card)
        fct = card.factor / 1000
        ivl2 = self._constrainedIvl((card.ivl + delay // 4) * 1.2, conf, card.ivl)
        ivl3 = self._constrainedIvl((card.ivl + delay // 2) * fct, conf, ivl2)
        ivl4 = self._constrainedIvl(
            (card.ivl + delay) * fct * conf['ease4'], conf, ivl3)
        if ease == 2:
            interval = ivl2
        elif ease == 3:
            interval = ivl3
        elif ease == 4:
            interval = ivl4
        # interval capped?
        return min(interval, conf['maxIvl'])

    def _fuzzedIvl(self, ivl):
        """
        给定一个间隔天数，根据间隔天数的大小，返回值附近的一个随机间隔天数。

        :param ivl: 间隔天数
        :return: 模糊的间隔天数
        """
        min, max = self._fuzzIvlRange(ivl)
        return random.randint(min, max)

    def _fuzzIvlRange(self, ivl):
        """
        返回模糊间隔天数范围

        :param ivl:间隔天数
        :return:间隔天数范围
        """
        if ivl < 2:
            return [1, 1]
        elif ivl == 2:
            return [2, 3]
        elif ivl < 7:
            fuzz = int(ivl*0.25)
        elif ivl < 30:
            fuzz = max(2, int(ivl*0.15))
        else:
            fuzz = max(4, int(ivl*0.05))
        # fuzz at least a day
        fuzz = max(fuzz, 1)
        return [ivl-fuzz, ivl+fuzz]

    def _constrainedIvl(self, ivl, conf, prev):
        """
        约束间隔为整数
        如果有设置Options/Review/interval modifier，用它放大或缩小间隔。
        """
        "Integer interval after interval factor and prev+1 constraints applied."
        new = ivl * conf.get('ivlFct', 1)
        return int(max(new, prev+1))

    def _daysLate(self, card):
        """Number of days later than scheduled."""
        due = card.odue if card.odid else card.due
        return max(0, self.today - due)

    def _updateRevIvl(self, card, ease):
        """
        根据回答更新间隔天数。

        并保证至少比之前的间隔天数大1并小于最大间隔天数。
        最大间隔天数在Options/Reviews/Maximum interval中设置。
        """

        idealIvl = self._nextRevIvl(card, ease)
        card.ivl = min(max(self._adjRevIvl(card, idealIvl), card.ivl+1),
                       self._revConf(card)['maxIvl'])

    def _adjRevIvl(self, card, idealIvl):
        """
        对精确的间隔天数做模糊处理。

        :param card:卡片
        :param idealIvl:理想的间隔天数
        :return:模糊处理后的间隔天数
        """
        if self._spreadRev:
            idealIvl = self._fuzzedIvl(idealIvl)
        return idealIvl

    # Dynamic deck handling
    ##########################################################################

    def rebuildDyn(self, did=None):
        """
        重建动态牌组。

        :param did: 牌组ID，要从哪个牌组重建动态牌组，默认为当前选择牌组。
        :return:
        """
        "Rebuild a dynamic deck."
        did = did or self.col.decks.selected()
        deck = self.col.decks.get(did)

        # 不能在动态牌组上创建动态牌组。
        assert deck['dyn']

        # 先将动态牌组中的卡片发还，再填充动态牌组。
        # move any existing cards back first, then fill
        self.emptyDyn(did)
        ids = self._fillDyn(deck)
        if not ids:
            return
        # and change to our new deck
        self.col.decks.select(did)
        return ids

    def _fillDyn(self, deck):
        """
        填充动态牌组
        :param deck:
        :return:
        """
        # 获取这个动态牌组的设置。
        search, limit, order = deck['terms'][0]

        # 获取查询语句
        orderlimit = self._dynOrder(order, limit)
        if search.strip():
            search = "(%s)" % search
        search = "%s -is:suspended -is:buried -deck:filtered -is:learn" % search
        try:
            ids = self.col.findCards(search, order=orderlimit)
        except:
            ids = []
            return ids

        # 将找到的卡片放进动态牌组中。
        # move the cards over
        self.col.log(deck['id'], ids)
        self._moveToDyn(deck['id'], ids)
        return ids

    def emptyDyn(self, did, lim=None):
        """
        清空动态牌组，将其中的卡片移回到原来的牌组中。

        :param did: 动态牌组ID。
        :param lim: 限制表达式。
        :return: 无。
        """
        if not lim:
            lim = "did = %s" % did
        self.col.log(self.col.db.list("select id from cards where %s" % lim))

        # 卡片牌组ID 还原
        # 正在学习的卡片放到新卡片队列，并变成新卡片。
        # 其它卡片是什么类型回什么队列，类型不变。
        # move out of cram queue
        self.col.db.execute("""
update cards set did = odid, queue = (case when type = 1 then 0
else type end), type = (case when type = 1 then 0 else type end),
due = odue, odue = 0, odid = 0, usn = ? where %s""" % lim,
                            self.col.usn())

    def remFromDyn(self, cids):
        """
        将卡片从动态牌组中移回原来的牌组。

        :param cids:卡片ID列表。
        :return:无
        """
        self.emptyDyn(None, "id in %s and odid" % ids2str(cids))

    def _dynOrder(self, o, l):
        """
        根据动态牌组选择卡片的方法和卡片数返回查询语句。

        :param o:选择卡片的方法。
        :param l:选择卡片的数量。
        :return:返回查询语句。
        """
        if o == DYN_OLDEST:
            t = "(select max(id) from revlog where cid=c.id)"
        elif o == DYN_RANDOM:
            t = "random()"
        elif o == DYN_SMALLINT:
            t = "ivl"
        elif o == DYN_BIGINT:
            t = "ivl desc"
        elif o == DYN_LAPSES:
            t = "lapses desc"
        elif o == DYN_ADDED:
            t = "n.id"
        elif o == DYN_REVADDED:
            t = "n.id desc"
        elif o == DYN_DUE:
            t = "c.due"
        elif o == DYN_DUEPRIORITY:
            t = "(case when queue=2 and due <= %d then (ivl / cast(%d-due+0.001 as real)) else 100000+due end)" % (
                    self.today, self.today)
        else:
            # if we don't understand the term, default to due order
            t = "c.due"
        return t + " limit %d" % l

    def _moveToDyn(self, did, ids):
        """
        将卡片移动到动态牌组中。

        :param did: 动态牌组的ID。
        :param ids: 卡片ID列表。
        :return:无
        """
        deck = self.col.decks.get(did)
        data = []
        t = intTime()
        u = self.col.usn()

        # 生成data要修改的数据列表，格式为（牌组ID, due, usn, 卡片ID）
        # due 从 -100000开始，这样所有临时牌组的卡片都是到期的。
        for c, id in enumerate(ids):
            # start at -100000 so that reviews are all due
            data.append((did, -100000+c, u, id))

        # 当卡片类型为复习卡片且到期的卡片放到复习卡片队列，否则放在新卡片队列。
        #
        # 到期的：
        #   odue不为0时，odue <= 当天
        #   odue为0时，due <= 当天
        # due reviews stay in the review queue. careful: can't use
        # "odid or did", as sqlite converts to boolean
        queue = """
(case when type=2 and (case when odue then odue <= %d else due <= %d end)
 then 2 else 0 end)"""
        queue %= (self.today, self.today)

        # 将上面的数据写入cards表中。
        self.col.db.executemany("""
update cards set
odid = (case when odid then odid else did end),
odue = (case when odue then odue else due end),
did = ?, queue = %s, due = ?, usn = ? where id = ?""" % queue, data)

    def _dynIvlBoost(self, card):
        """为临时牌组中的复习卡片计算新的间隔天数。"""
        assert card.odid and card.type == 2
        assert card.factor

        # 原间隔天数中剩下的天数
        elapsed = card.ivl - (card.odue - self.today)

        # factor初始值为STARTING_FACTOR（2500）。
        # 算法：
        #   - 新的间隔天数 = ((card.factor/1000)+1.2)/2 * 剩余间隔天数
        #   - 新的间隔天数不超过原来的间隔天数且不小于1天。
        # 注意：
        #  计算用的因子会倾向1.2，也就是：
        #   - 如果卡片的因子<1.2，因子会放大一些。
        #   - 如果卡片的因子>1.2，因子会缩小一些。
        factor = ((card.factor/1000)+1.2)/2
        ivl = int(max(card.ivl, elapsed * factor, 1))
        conf = self._revConf(card)
        return min(conf['maxIvl'], ivl)

    # Leeches
    ##########################################################################

    def _checkLeech(self, card, conf):
        """
        检查卡片是否是难点。

        如果一张卡回答Again的次数太多，当超过了Options/Lapses/Leech threshold的值
        或之后每超过极限值的一半时，就将卡片添加leech标签，表示是难点。
        """


        "Leech handler. True if card was a leech."
        lf = conf['leechFails']
        if not lf:
            return
        # if over threshold or every half threshold reps after that
        if (card.lapses >= lf and
            (card.lapses-lf) % (max(lf // 2, 1)) == 0):
            # add a leech tag
            f = card.note()
            f.addTag("leech")
            f.flush()

            # 执行标记为难点时的动作，
            # 在Options/Lapses/Leech action中设置。
            # handle
            a = conf['leechAction']
            if a == 0:
                # 移动卡片到休息队列中。
                # if it has an old due, remove it from cram/relearning
                if card.odue:
                    card.due = card.odue
                if card.odid:
                    card.did = card.odid
                card.odue = card.odid = 0
                card.queue = -1
            # notify UI
            runHook("leech", card)
            return True

    # Tools
    ##########################################################################

    def _cardConf(self, card):
        """获取卡片所在牌组的设置。"""
        return self.col.decks.confForDid(card.did)

    def _newConf(self, card):
        """
        获取卡片的新卡片设置。

        :param card: 卡片。
        :return: 新卡片的设置。
        """

        # 获取卡片所在牌组的设置。
        conf = self._cardConf(card)

        # 如果是一般的牌组，直接返回Options/New cards的设置。
        # normal deck
        if not card.odid:
            return conf['new']

        # 对于临时牌组的卡片，则取临时牌组的设置，其余设置使用旧牌组的设置。
        # dynamic deck; override some attributes, use original deck for others
        oconf = self.col.decks.confForDid(card.odid)
        delays = conf['delays'] or oconf['new']['delays']
        return dict(
            # original deck
            ints=oconf['new']['ints'],
            initialFactor=oconf['new']['initialFactor'],
            bury=oconf['new'].get("bury", True),
            # overrides
            delays=delays,
            separate=conf['separate'],
            order=NEW_CARDS_DUE,
            perDay=self.reportLimit
        )

    def _lapseConf(self, card):
        """
        获取卡片的遗忘设置。

        :param card: 卡片。
        :return: 遗忘设置。
        """
        conf = self._cardConf(card)

        # 如果是一般的牌组，直接返回Options/Lapse的设置。
        # normal deck
        if not card.odid:
            return conf['lapse']

        # 对于临时牌组的卡片，则取临时牌组的设置，其余设置使用旧牌组的设置。
        # dynamic deck; override some attributes, use original deck for others
        oconf = self.col.decks.confForDid(card.odid)
        delays = conf['delays'] or oconf['lapse']['delays']
        return dict(
            # original deck
            minInt=oconf['lapse']['minInt'],
            leechFails=oconf['lapse']['leechFails'],
            leechAction=oconf['lapse']['leechAction'],
            mult=oconf['lapse']['mult'],
            # overrides
            delays=delays,
            resched=conf['resched'],
        )

    def _revConf(self, card):
        """
        获取卡片的复习设置。

        :param card: 卡片。
        :return: 返回复习设置。
        """
        conf = self._cardConf(card)

        # 如果是一般的牌组，直接返回Options/Reviews的设置。
        # normal deck
        if not card.odid:
            return conf['rev']

        # 对于临时牌组的卡片，则使用旧牌组的复习设置，也就是Options/Reviews设置。
        # dynamic deck
        return self.col.decks.confForDid(card.odid)['rev']

    def _deckLimit(self):
        """获取活动牌组ID列表。"""
        return ids2str(self.col.decks.active())

    def _resched(self, card):
        """
        卡片是否需要重新安排复习日期（排期）。

        :param card:卡片
        :return: True-要，False-不要。
        """

        # 读取卡片的配置，如果这个卡片不是临时牌组中的卡片,
        # 那么肯定是要重新排期的，返回True。
        # conf['dyn'] = True表示是临时牌组中的卡片。
        conf = self._cardConf(card)
        if not conf['dyn']:
            return True

        # 如果是临时牌组中的卡片，那么就要看临时牌组设置中的
        # Reschedule card base on my answer in this deck是否勾选上。
        # 勾选上表示临时牌组的卡片发还到原牌组时，是否要根据此卡在
        # 临时牌组中的回答来重新排期。
        # conf['resched']表示这个选项的值。
        return conf['resched']

    # Daily cutoff
    ##########################################################################

    def _updateCutoff(self):
        """更新当日截止时间，出现跨日期的情况时清零日内计数，还原被搁置的卡片。"""

        # 保存today旧值
        oldToday = self.today

        # 重新计算today：从牌组集创建到现在,共有多少天。
        # days since col created
        self.today = int((time.time() - self.col.crt) // 86400)

        # 当天截止时间。
        # end of day cutoff
        self.dayCutoff = self.col.crt + (self.today+1)*86400

        # 跨天
        if oldToday != self.today:
            self.col.log(self.today, self.dayCutoff)

        # 如果跨日，清零所有牌组的日内计数。
        # update all daily counts, but don't save decks to prevent needless
        # conflicts. we'll save on card answer instead
        def update(g):
            for t in "new", "rev", "lrn", "time":
                key = t+"Today"
                if g[key][0] != self.today:
                    g[key] = [self.today, 0]
        for deck in self.col.decks.all():
            update(deck)

        # 如果跨日，还原所有被搁置的卡。
        # unbury if the day has rolled over
        unburied = self.col.conf.get("lastUnburied", 0)
        if unburied < self.today:
            self.unburyCards()

    def _checkDay(self):
        """如果时间跨越当日截止时间，那么重置调度器。"""

        # check if the day has rolled over
        if time.time() > self.dayCutoff:
            self.reset()

    # Deck finished state
    ##########################################################################

    def finishedMsg(self):
        """返回当日完成所有事情时的html文本。"""
        return ("<b>"+_(
            "Congratulations! You have finished this deck for now.")+
            "</b><br><br>" + self._nextDueMsg())

    def _nextDueMsg(self):
        line = []

        # 通知用户已经达到复习卡片数量限制，但还有到期的卡片需要复习。
        # the new line replacements are so we don't break translations
        # in a point release
        if self.revDue():
            line.append(_("""\
Today's review limit has been reached, but there are still cards
waiting to be reviewed. For optimum memory, consider increasing
the daily limit in the options.""").replace("\n", " "))
        # 通知用户已经达到新卡片数量限制，但还有新卡片可用。
        if self.newDue():
            line.append(_("""\
There are more new cards available, but the daily limit has been
reached. You can increase the limit in the options, but please
bear in mind that the more new cards you introduce, the higher
your short-term review workload will become.""").replace("\n", " "))
        # 提示用户有被搁置的卡。
        if self.haveBuried():
            if self.haveCustomStudy:
                now = " " +  _("To see them now, click the Unbury button below.")
            else:
                now = ""
            line.append(_("""\
Some related or buried cards were delayed until a later session.""")+now)
        # 当前牌组不是临时牌组时，还会提醒用户可以通过创建临时
        # 牌组，在正常的学习进度之外进行定制学习。
        if self.haveCustomStudy and not self.col.decks.current()['dyn']:
            line.append(_("""\
To study outside of the normal schedule, click the Custom Study button below."""))
        return "<p>".join(line)

    def revDue(self):
        """活动牌组中的复习队列中还有到期的卡片时返回True。"""
        "True if there are any rev cards due."
        return self.col.db.scalar(
            ("select 1 from cards where did in %s and queue = 2 "
             "and due <= ? limit 1") % self._deckLimit(),
            self.today)

    def newDue(self):
        """活动牌组中的新卡片队列中还有卡片时返回True。"""
        "True if there are any new cards due."
        return self.col.db.scalar(
            ("select 1 from cards where did in %s and queue = 0 "
             "limit 1") % self._deckLimit())

    def haveBuried(self):
        """活动牌组中的搁置队列中还有卡片时返回True。"""
        sdids = ids2str(self.col.decks.active())
        cnt = self.col.db.scalar(
            "select 1 from cards where queue = -2 and did in %s limit 1" % sdids)
        return not not cnt

    # Next time reports
    ##########################################################################

    def nextIvlStr(self, card, ease, short=False):
        "Return the next interval for CARD as a string."
        ivl = self.nextIvl(card, ease)
        if not ivl:
            return _("(end)")
        s = fmtTimeSpan(ivl, short=short)
        if ivl < self.col.conf['collapseTime']:
            s = "<"+s
        return s

    def nextIvl(self, card, ease):
        """
        当回答一张卡片后，卡片下一次出现的间隔，以秒为单位。

        :param card: 卡片。
        :param ease: 回答
        :return: 卡片下一次出现的间隔，以秒为单位 。
        """

        "Return the next interval for CARD, in seconds."
        if card.queue in (0,1,3):
            # 卡片在新卡片队列、学习队列或日学习队列中时
            # 返回学习卡片的下一个间隔。
            return self._nextLrnIvl(card, ease)
        elif ease == 1:
            # 复习队列中的卡片回答遗忘时回到遗忘设置的学习步骤的第一步。
            # 如果遗忘设置的学习步骤为空时，返回被削减的间隔天数。
            # lapsed
            conf = self._lapseConf(card)
            if conf['delays']:
                return conf['delays'][0]*60
            return self._nextLapseIvl(card, conf)*86400
        else:
            # 复习队列中的卡片正常情况下的间隔。
            # review
            return self._nextRevIvl(card, ease)*86400

    # this isn't easily extracted from the learn code
    def _nextLrnIvl(self, card, ease):
        """
        根据对学习卡片的回答，返回卡片下一次出现的间隔。
        从_answerLrnCard中提取出来的算法。

        :param card:卡片。
        :param ease: 回答。
        :return: 卡片下一次出现的间隔。返回0表示卡片不用排期。
        """

        # 卡片是新卡片，获取卡片的初始步数。
        if card.queue == 0:
            card.left = self._startingLeft(card)

        # 获取学习配置。
        conf = self._lrnConf(card)
        if ease == 1:
            # 回答Again时，退回学习的第一步，返回第一步的时间间隔。
            # fail
            return self._delayForGrade(conf, len(conf['delays']))
        elif ease == 3:
            # 回答Easy时，跳过所有学习步骤，立即完成学习。
            # early removal
            if not self._resched(card):
                # 不用的排期的卡，返回0。
                return 0

            # 完成所有学习步骤的第一次排期时的间隔。
            return self._graduatingIvl(card, conf, True, adj=False) * 86400
        else:
            # 下一个学习步骤。
            left = card.left%1000 - 1
            if left <= 0:
                # 所有学习步骤完成，完成学习。
                # graduate
                if not self._resched(card):
                    # 不用的排期的卡，返回0。
                    return 0

                # 完成所有学习步骤的第一次排期时的间隔。
                return self._graduatingIvl(card, conf, False, adj=False) * 86400
            else:
                # 当前学习步骤的间隔。
                return self._delayForGrade(conf, left)

    # Suspending
    ##########################################################################

    def suspendCards(self, ids):
        """
        休眠卡片。

        将卡片从动态牌组中发还到原来的牌组。
        将正在学习的卡片发还到它们原来的队列中。
        最后将卡片移到休眠卡片队列中。

        :param ids: 要休眠的卡片的ID列表。
        :return: 无。
        """

        "Suspend cards."
        self.col.log(ids)
        self.remFromDyn(ids)
        self.removeLrn(ids)
        self.col.db.execute(
            "update cards set queue=-1,mod=?,usn=? where id in "+
            ids2str(ids), intTime(), self.col.usn())

    def unsuspendCards(self, ids):
        """
        撤消卡片的休眠状态。根据卡片的类型，将卡片移动到相应的队列中。

        :param ids: 要休眠的卡片的ID列表。
        :return: 无。
        """

        "Unsuspend cards."
        self.col.log(ids)
        self.col.db.execute(
            "update cards set queue=type,mod=?,usn=? "
            "where queue = -1 and id in "+ ids2str(ids),
            intTime(), self.col.usn())

    def buryCards(self, cids):
        """
        搁置卡片。

        将卡片从动态牌组中发还到原来的牌组。
        将正在学习的卡片发还到它们原来的队列中。
        最后将卡片移到搁置卡片队列中。

        :param ids: 要搁置的卡片的ID列表。
        :return: 无。
        """

        self.col.log(cids)
        self.remFromDyn(cids)
        self.removeLrn(cids)
        self.col.db.execute("""
update cards set queue=-2,mod=?,usn=? where id in """+ids2str(cids),
                            intTime(), self.col.usn())

    def buryNote(self, nid):
        """
        搁置一条笔记的所有卡片。

        将卡片从动态牌组中发还到原来的牌组。
        将正在学习的卡片发还到它们原来的队列中。
        最后将卡片移到搁置卡片队列中。

        :param ids: 笔记ID。
        :return: 无。
        """

        "Bury all cards for note until next session."
        cids = self.col.db.list(
            "select id from cards where nid = ? and queue >= 0", nid)
        self.buryCards(cids)

    # Sibling spacing
    ##########################################################################

    def _burySiblings(self, card):
        """
        搁置card的兄弟卡片（同一笔记下的其它卡片）。

        详细查看文档的Siblings and Burying小节。
        :param card: 卡片。
        :return: 无。
        """
        toBury = []

        # 获取options/new card/bury的设置
        nconf = self._newConf(card)
        buryNew = nconf.get("bury", True)

        # 获取options/review/bury的设置
        rconf = self._revConf(card)
        buryRev = rconf.get("bury", True)

        # 从数据库的cards表中找出新卡片队列的兄弟卡片
        # 和复习卡片队列中且已经到期的兄弟卡片。
        # 兄弟卡片是指同一个笔记下的其它卡片。
        # 将这些卡片从排期器的新卡片队列_newQueue或复习卡片队列_revQueue中移除。
        # 同时修改cards表将这些卡片在移动到搁置卡片队列中。

        # loop through and remove from queues
        for cid,queue in self.col.db.execute("""
select id, queue from cards where nid=? and id!=?
and (queue=0 or (queue=2 and due<=?))""",
                card.nid, card.id, self.today):
            if queue == 2:
                if buryRev:
                    toBury.append(cid)
                # if bury disabled, we still discard to give same-day spacing
                try:
                    self._revQueue.remove(cid)
                except ValueError:
                    pass
            else:
                # if bury disabled, we still discard to give same-day spacing
                if buryNew:
                    toBury.append(cid)
                try:
                    self._newQueue.remove(cid)
                except ValueError:
                    pass
        # then bury
        if toBury:
            self.col.db.execute(
                "update cards set queue=-2,mod=?,usn=? where id in "+ids2str(toBury),
                intTime(), self.col.usn())
            self.col.log(toBury)

    # Resetting
    ##########################################################################

    def forgetCards(self, ids):
        """
        将卡片设置成新卡片并放回的新卡片队列的最后。

        :param ids:卡片ID列表。
        :return: 无。
        """

        "Put cards at the end of the new queue."
        # 将卡片从动态牌组中移回原来的牌组。
        self.remFromDyn(ids)

        # 设置卡片类型为新卡片，新卡片队列，相关调度数据复位。
        self.col.db.execute(
            "update cards set type=0,queue=0,ivl=0,due=0,odue=0,factor=?"
            " where id in "+ids2str(ids), STARTING_FACTOR)

        # 将这些卡片放在新卡片的最后。
        pmax = self.col.db.scalar(
            "select max(due) from cards where type=0") or 0
        # takes care of mod + usn
        self.sortCards(ids, start=pmax+1)
        self.col.log(ids)

    def reschedCards(self, ids, imin, imax):
        """
        将卡片放入复习队列中，使用新的间隔天数（最小值和最大值）。

        :param ids: 卡片的ID列表。
        :param imin: 最小间隔天数。
        :param imax: 最大间隔天数。
        :return: 无。
        """

        "Put cards in review queue with a new interval in days (min, max)."
        d = []
        t = self.today
        mod = intTime()
        for id in ids:
            r = random.randint(imin, imax)
            d.append(dict(id=id, due=r+t, ivl=max(1, r), mod=mod,
                          usn=self.col.usn(), fact=STARTING_FACTOR))
        self.remFromDyn(ids)
        self.col.db.executemany("""
update cards set type=2,queue=2,ivl=:ivl,due=:due,odue=0,
usn=:usn,mod=:mod,factor=:fact where id=:id""",
                                d)
        self.col.log(ids)

    def resetCards(self, ids):
        """
        完全复位卡片用于导出。

        :param ids: 卡片ID列表。
        :return: 无。
        """

        "Completely reset cards for export."
        sids = ids2str(ids)

        # 找出这些卡片中不是新卡片的卡片。
        # we want to avoid resetting due number of existing new cards on export
        nonNew = self.col.db.list(
            "select id from cards where id in %s and (queue != 0 or type != 0)"
            % sids)

        # 重置所有卡片。
        # reset all cards
        self.col.db.execute(
            "update cards set reps=0,lapses=0,odid=0,odue=0,queue=0"
            " where id in %s" % sids
        )

        # 非新卡片全部设置回新卡片。
        # and forget any non-new cards, changing their due numbers
        self.forgetCards(nonNew)
        self.col.log(ids)

    # Repositioning new cards
    ##########################################################################

    def sortCards(self, cids, start=1, step=1, shuffle=False, shift=False):
        """
        新卡片排序。

        :param cids: 要排序的卡片ID列表
        :param start: due的开始值
        :param step: due的步长
        :param shuffle: 是否打乱顺序
        :param shift: 在原有卡片之间插入，会改变插入点后面卡片的due。
        :return:
        """
        # 找出这些卡片的笔记ID到nids
        scids = ids2str(cids)
        now = intTime()
        nids = []
        nidsSet = set()
        for id in cids:
            nid = self.col.db.scalar("select nid from cards where id = ?", id)
            if nid not in nidsSet:
                nids.append(nid)
                nidsSet.add(nid)
        if not nids:
            # no new cards
            return

        # 确定卡片的顺序：随机或按笔记的ID顺序
        # determine nid ordering
        due = {}
        if shuffle:
            random.shuffle(nids)
        for c, nid in enumerate(nids):
            due[nid] = start+c*step
        high = start+c*step

        # 在原有卡片之间插入。
        # shift?
        if shift:
            # 找出插入位置low
            low = self.col.db.scalar(
                "select min(due) from cards where due >= ? and type = 0 "
                "and id not in %s" % scids,
                start)
            if low is not None:
                # 插入位置后面的卡片due往后移动
                shiftby = high - low + 1
                self.col.db.execute("""
update cards set mod=?, usn=?, due=due+? where id not in %s
and due >= ? and queue = 0""" % scids, now, self.col.usn(), shiftby, low)

        # 将这些卡片改到正确的位置，即due值。
        # reorder cards
        d = []
        for id, nid in self.col.db.execute(
            "select id, nid from cards where type = 0 and id in "+scids):
            d.append(dict(now=now, due=due[nid], usn=self.col.usn(), cid=id))
        self.col.db.executemany(
            "update cards set due=:due,mod=:now,usn=:usn where id = :cid", d)

    def randomizeCards(self, did):
        """
        对did牌组中的所有新卡片打乱顺序。

        :param did: 牌组ID。
        :return: 无。
        """

        cids = self.col.db.list("select id from cards where did = ?", did)
        self.sortCards(cids, shuffle=True)

    def orderCards(self, did):
        """
        对did牌组中的所有新卡片排序。

        :param did: 牌组ID。
        :return: 无。
        """
        cids = self.col.db.list("select id from cards where did = ? order by id", did)
        self.sortCards(cids)

    def resortConf(self, conf):
        """
        找出所有使用conf配置的牌组，根据配置中的设置对牌组中的新卡片排序。

        :param conf: 配置。
        :return: 无。
        """
        for did in self.col.decks.didsForConf(conf):
            if conf['new']['order'] == 0:
                self.randomizeCards(did)
            else:
                self.orderCards(did)

    # for post-import
    def maybeRandomizeDeck(self, did=None):
        """检查Options/New cards/Order设置，是否需要打乱牌组中新卡片的顺序，如果要则打乱牌组中新卡片的顺序"""
        if not did:
            did = self.col.decks.selected()
        conf = self.col.decks.confForDid(did)
        # in order due?
        if conf['new']['order'] == NEW_CARDS_RANDOM:
            self.randomizeCards(did)
