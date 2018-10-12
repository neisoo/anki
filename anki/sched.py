# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
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

class Scheduler:
    name = "std"
    haveCustomStudy = True
    _spreadRev = True
    _burySiblingsOnAnswer = True

    def __init__(self, col):
        """构造排期器，基于一个collection。"""
        self.col = col
        self.queueLimit = 50
        self.reportLimit = 1000
        self.reps = 0
        self.today = None
        self._haveQueues = False
        self._updateCutoff()

    def getCard(self):
        """从队列中取出下一张要回答的卡片，没有卡片时返回空。"""
        "Pop the next card from the queue. None if finished."
        self._checkDay()
        if not self._haveQueues:
            self.reset()
        card = self._getCard()
        if card:
            self.col.log(card)
            if not self._burySiblingsOnAnswer:
                self._burySiblings(card)
            self.reps += 1
            card.startTimer()
            return card

    def reset(self):
        self._updateCutoff()
        self._resetLrn()
        self._resetRev()
        self._resetNew()
        self._haveQueues = True

    def answerCard(self, card, ease):
        """回答getCard()取出的卡片并放回队列中。"""
        self.col.log()
        assert 1 <= ease <= 4
        self.col.markReview(card)

        # 隐藏同一笔记下的其它卡片，如果用户有设置的话
        if self._burySiblingsOnAnswer:
            self._burySiblings(card)

        # 累加卡片复习次数
        card.reps += 1

        # former is for logging new cards, latter also covers filt. decks
        card.wasNew = card.type == 0
        wasNewQ = card.queue == 0
        if wasNewQ:
            # 新卡片队列中的卡片

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
            if card.odid and card.type == 2:
                if self._resched(card):
                    # reviews get their ivl boosted on first sight
                    card.ivl = self._dynIvlBoost(card)
                    card.odue = self.today + card.ivl

            # 增加卡片所在牌组和其所有父牌组的'新卡片'计数
            self._updateStats(card, 'new')

        if card.queue in (1, 3):
            # 回答学习队列中的卡片
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
        card.mod = intTime()
        card.usn = self.col.usn()
        card.flushSched()

    def counts(self, card=None):
        counts = [self.newCount, self.lrnCount, self.revCount]
        if card:
            idx = self.countIdx(card)
            if idx == 1:
                counts[1] += card.left // 1000
            else:
                counts[idx] += 1
        return tuple(counts)

    def dueForecast(self, days=7):
        "Return counts over next DAYS. Includes today."
        daysd = dict(self.col.db.all("""
select due, count() from cards
where did in %s and queue = 2
and due between ? and ?
group by due
order by due""" % self._deckLimit(),
                            self.today,
                            self.today+days-1))
        for d in range(days):
            d = self.today+d
            if d not in daysd:
                daysd[d] = 0
        # return in sorted order
        ret = [x[1] for x in sorted(daysd.items())]
        return ret

    def countIdx(self, card):
        if card.queue == 3:
            return 1
        return card.queue

    def answerButtons(self, card):
        if card.odue:
            # normal review in dyn deck?
            if card.odid and card.queue == 2:
                return 4
            conf = self._lrnConf(card)
            if card.type in (0,1) or len(conf['delays']) > 1:
                return 3
            return 2
        elif card.queue == 2:
            return 4
        else:
            return 3

    def unburyCards(self):
        "Unbury cards."
        self.col.conf['lastUnburied'] = self.today
        self.col.log(
            self.col.db.list("select id from cards where queue = -2"))
        self.col.db.execute(
            "update cards set queue=type where queue = -2")

    def unburyCardsForDeck(self):
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
        tot = 0
        pcounts = {}
        # for each of the active decks
        nameMap = self.col.decks.nameMap()
        for did in self.col.decks.active():
            # early alphas were setting the active ids as a str
            did = int(did)
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
            # see how many cards we actually have
            cnt = cntFn(did, lim)
            # if non-zero, decrement from parent counts
            for p in parents:
                pcounts[p['id']] -= cnt
            # we may also be a parent
            pcounts[did] = lim - cnt
            # and add to running total
            tot += cnt
        return tot

    # Deck list
    ##########################################################################

    def deckDueList(self):
        "Returns [deckname, did, rev, lrn, new]"
        self._checkDay()
        self.col.decks.recoverOrphans()
        decks = self.col.decks.all()
        decks.sort(key=itemgetter('name'))
        lims = {}
        data = []
        def parent(name):
            parts = name.split("::")
            if len(parts) < 2:
                return None
            parts = parts[:-1]
            return "::".join(parts)
        for deck in decks:
            # if we've already seen the exact same deck name, rename the
            # invalid duplicate and reload
            if deck['name'] in lims:
                deck['name'] += "1"
                self.col.decks.save(deck)
                return self.deckDueList()
            # ensure no sections are blank
            if not all(deck['name'].split("::")):
                deck['name'] = "recovered"
                self.col.decks.save(deck)
                return self.deckDueList()

            p = parent(deck['name'])
            # new
            nlim = self._deckNewLimitSingle(deck)
            if p:
                if p not in lims:
                    # if parent was missing, this deck is invalid
                    deck['name'] = "recovered"
                    self.col.decks.save(deck)
                    return self.deckDueList()
                nlim = min(nlim, lims[p][0])
            new = self._newForDeck(deck['id'], nlim)
            # learning
            lrn = self._lrnForDeck(deck['id'])
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
        return self._groupChildren(self.deckDueList())

    def _groupChildren(self, grps):
        # first, split the group names into components
        for g in grps:
            g[0] = g[0].split("::")
        # and sort based on those components
        grps.sort(key=itemgetter(0))
        # then run main function
        return self._groupChildrenMain(grps)

    def _groupChildrenMain(self, grps):
        tree = []
        # group and recurse
        def key(grp):
            return grp[0][0]
        for (head, tail) in itertools.groupby(grps, key=key):
            tail = list(tail)
            did = None
            rev = 0
            new = 0
            lrn = 0
            children = []
            for c in tail:
                if len(c[0]) == 1:
                    # current node
                    did = c[1]
                    rev += c[2]
                    lrn += c[3]
                    new += c[4]
                else:
                    # set new string to tail
                    c[0] = c[0][1:]
                    children.append(c)
            children = self._groupChildrenMain(children)
            # tally up children counts
            for ch in children:
                rev += ch[2]
                lrn += ch[3]
                new += ch[4]
            # limit the counts to the deck's limits
            conf = self.col.decks.confForDid(did)
            deck = self.col.decks.get(did)
            if not conf['dyn']:
                rev = max(0, min(rev, conf['rev']['perDay']-deck['revToday'][1]))
                new = max(0, min(new, conf['new']['perDay']-deck['newToday'][1]))
            tree.append((head, did, rev, lrn, new, children))
        return tuple(tree)

    # Getting the next card
    ##########################################################################

    def _getCard(self):
        "Return the next due card id, or None."
        # learning card due?
        c = self._getLrnCard()
        if c:
            return c
        # new first, or time for one?
        if self._timeForNewCard():
            c = self._getNewCard()
            if c:
                return c
        # card due for review?
        c = self._getRevCard()
        if c:
            return c
        # day learning card due?
        c = self._getLrnDayCard()
        if c:
            return c
        # new cards left?
        c = self._getNewCard()
        if c:
            return c
        # collapse or finish
        return self._getLrnCard(collapse=True)

    # New cards
    ##########################################################################

    def _resetNewCount(self):
        cntFn = lambda did, lim: self.col.db.scalar("""
select count() from (select 1 from cards where
did = ? and queue = 0 limit ?)""", did, lim)
        self.newCount = self._walkingCount(self._deckNewLimitSingle, cntFn)

    def _resetNew(self):
        self._resetNewCount()
        self._newDids = self.col.decks.active()[:]
        self._newQueue = []
        self._updateNewCardRatio()

    def _fillNew(self):
        if self._newQueue:
            return True
        if not self.newCount:
            return False
        while self._newDids:
            did = self._newDids[0]
            lim = min(self.queueLimit, self._deckNewLimit(did))
            if lim:
                # fill the queue with the current did
                self._newQueue = self.col.db.list("""
select id from cards where did = ? and queue = 0 order by due limit ?""", did, lim)
                if self._newQueue:
                    self._newQueue.reverse()
                    return True
            # nothing left in the deck; move to next
            self._newDids.pop(0)
        if self.newCount:
            # if we didn't get a card but the count is non-zero,
            # we need to check again for any cards that were
            # removed from the queue but not buried
            self._resetNew()
            return self._fillNew()

    def _getNewCard(self):
        if self._fillNew():
            self.newCount -= 1
            return self.col.getCard(self._newQueue.pop())

    def _updateNewCardRatio(self):
        if self.col.conf['newSpread'] == NEW_CARDS_DISTRIBUTE:
            if self.newCount:
                self.newCardModulus = (
                    (self.newCount + self.revCount) // self.newCount)
                # if there are cards to review, ensure modulo >= 2
                if self.revCount:
                    self.newCardModulus = max(2, self.newCardModulus)
                return
        self.newCardModulus = 0

    def _timeForNewCard(self):
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
        if not fn:
            fn = self._deckNewLimitSingle
        sel = self.col.decks.get(did)
        lim = -1
        # for the deck and each of its parents
        for g in [sel] + self.col.decks.parents(did):
            rem = fn(g)
            if lim == -1:
                lim = rem
            else:
                lim = min(rem, lim)
        return lim

    def _newForDeck(self, did, lim):
        "New count for a single deck."
        if not lim:
            return 0
        lim = min(lim, self.reportLimit)
        return self.col.db.scalar("""
select count() from
(select 1 from cards where did = ? and queue = 0 limit ?)""", did, lim)

    def _deckNewLimitSingle(self, g):
        "Limit for deck without parent limits."
        if g['dyn']:
            return self.reportLimit
        c = self.col.decks.confForDid(g['id'])
        return max(0, c['new']['perDay'] - g['newToday'][1])

    def totalNewForCurrentDeck(self):
        return self.col.db.scalar(
            """
select count() from cards where id in (
select id from cards where did in %s and queue = 0 limit ?)"""
            % ids2str(self.col.decks.active()), self.reportLimit)

    # Learning queues
    ##########################################################################

    def _resetLrnCount(self):
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
        self._resetLrnCount()
        self._lrnQueue = []
        self._lrnDayQueue = []
        self._lrnDids = self.col.decks.active()[:]

    # sub-day learning
    def _fillLrn(self):
        if not self.lrnCount:
            return False
        if self._lrnQueue:
            return True
        self._lrnQueue = self.col.db.all("""
select due, id from cards where
did in %s and queue = 1 and due < :lim
limit %d""" % (self._deckLimit(), self.reportLimit), lim=self.dayCutoff)
        # as it arrives sorted by did first, we need to sort it
        self._lrnQueue.sort()
        return self._lrnQueue

    def _getLrnCard(self, collapse=False):
        if self._fillLrn():
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
        if not self.lrnCount:
            return False
        if self._lrnDayQueue:
            return True
        while self._lrnDids:
            did = self._lrnDids[0]
            # fill the queue with the current did
            self._lrnDayQueue = self.col.db.list("""
select id from cards where
did = ? and queue = 3 and due <= ? limit ?""",
                                    did, self.today, self.queueLimit)
            if self._lrnDayQueue:
                # order
                r = random.Random()
                r.seed(self.today)
                r.shuffle(self._lrnDayQueue)
                # is the current did empty?
                if len(self._lrnDayQueue) < self.queueLimit:
                    self._lrnDids.pop(0)
                return True
            # nothing left in the deck; move to next
            self._lrnDids.pop(0)

    def _getLrnDayCard(self):
        if self._fillLrnDay():
            self.lrnCount -= 1
            return self.col.getCard(self._lrnDayQueue.pop())

    def _answerLrnCard(self, card, ease):
        """
        回答正在学习的卡

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
                # 更新步数，低3位表示剩余步数，高位表示到今天截止能完成的步数。
                # decrement real left count and recalculate left today
                left = (card.left % 1000) - 1
                card.left = self._leftToday(conf['delays'], left)*1000 + left
            # 用户在学习卡片时回答Again，表示要回到第一步，重新学习。
            # failed
            else:
                # 初始化步数
                card.left = self._startingLeft(card)

                # 如果有'mult'设置且需要排期
                resched = self._resched(card)
                if 'mult' in conf and resched:
                    # review that's lapsed
                    card.ivl = max(1, conf['minInt'], card.ivl*conf['mult'])
                else:
                    # 新卡片在学习过程中回答Again时，不需要排期。
                    # new card; no ivl adjustment
                    pass

                # 需要排期并且是临时卡片组，旧排期改为明天。
                if resched and card.odid:
                    card.odue = self.today + 1
            delay = self._delayForGrade(conf, card.left)
            if card.due < time.time():
                # not collapsed; add some randomness
                delay *= random.uniform(1, 1.25)
            card.due = int(time.time() + delay)
            # due today?
            if card.due < self.dayCutoff:
                self.lrnCount += card.left // 1000
                # if the queue is not empty and there's nothing else to do, make
                # sure we don't put it at the head of the queue and end up showing
                # it twice in a row
                card.queue = 1
                if self._lrnQueue and not self.revCount and not self.newCount:
                    smallestDue = self._lrnQueue[0][0]
                    card.due = max(card.due, smallestDue+1)
                heappush(self._lrnQueue, (card.due, card.id))
            else:
                # the card is due in one or more days, so we need to use the
                # day learn queue
                ahead = ((card.due - self.dayCutoff) // 86400) + 1
                card.due = self.today + ahead
                card.queue = 3
        self._logLrn(card, ease, conf, leaving, type, lastLeft)

    def _delayForGrade(self, conf, left):
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
        if card.type == 2:
            return self._lapseConf(card)
        else:
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
        :return: 低三位为总步数，其余高位为从现在到当天截止前可以完成的步数，
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

        # Options/New cards/Starting ease的设置
        card.factor = conf['initialFactor']

    def _logLrn(self, card, ease, conf, leaving, type, lastLeft):
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
        "Remove cards from the learning queues."
        if ids:
            extra = " and id in "+ids2str(ids)
        else:
            # benchmarks indicate it's about 10x faster to search all decks
            # with the index than scan the table
            extra = " and did in "+ids2str(self.col.decks.allIds())
        # review cards in relearning
        self.col.db.execute("""
update cards set
due = odue, queue = 2, mod = %d, usn = %d, odue = 0
where queue in (1,3) and type = 2
%s
""" % (intTime(), self.col.usn(), extra))
        # new cards in learning
        self.forgetCards(self.col.db.list(
            "select id from cards where queue in (1,3) %s" % extra))

    def _lrnForDeck(self, did):
        cnt = self.col.db.scalar(
            """
select sum(left/1000) from
(select left from cards where did = ? and queue = 1 and due < ? limit ?)""",
            did, intTime() + self.col.conf['collapseTime'], self.reportLimit) or 0
        return cnt + self.col.db.scalar(
            """
select count() from
(select 1 from cards where did = ? and queue = 3
and due <= ? limit ?)""",
            did, self.today, self.reportLimit)

    # Reviews
    ##########################################################################

    def _deckRevLimit(self, did):
        return self._deckNewLimit(did, self._deckRevLimitSingle)

    def _deckRevLimitSingle(self, d):
        if d['dyn']:
            return self.reportLimit
        c = self.col.decks.confForDid(d['id'])
        return max(0, c['rev']['perDay'] - d['revToday'][1])

    def _revForDeck(self, did, lim):
        lim = min(lim, self.reportLimit)
        return self.col.db.scalar(
            """
select count() from
(select 1 from cards where did = ? and queue = 2
and due <= ? limit ?)""",
            did, self.today, lim)

    def _resetRevCount(self):
        def cntFn(did, lim):
            return self.col.db.scalar("""
select count() from (select id from cards where
did = ? and queue = 2 and due <= ? limit %d)""" % lim,
                                      did, self.today)
        self.revCount = self._walkingCount(
            self._deckRevLimitSingle, cntFn)

    def _resetRev(self):
        self._resetRevCount()
        self._revQueue = []
        self._revDids = self.col.decks.active()[:]

    def _fillRev(self):
        if self._revQueue:
            return True
        if not self.revCount:
            return False
        while self._revDids:
            did = self._revDids[0]
            lim = min(self.queueLimit, self._deckRevLimit(did))
            if lim:
                # fill the queue with the current did
                self._revQueue = self.col.db.list("""
select id from cards where
did = ? and queue = 2 and due <= ? limit ?""",
                                                  did, self.today, lim)
                if self._revQueue:
                    # ordering
                    if self.col.decks.get(did)['dyn']:
                        # dynamic decks need due order preserved
                        self._revQueue.reverse()
                    else:
                        # random order for regular reviews
                        r = random.Random()
                        r.seed(self.today)
                        r.shuffle(self._revQueue)
                    # is the current did empty?
                    if len(self._revQueue) < lim:
                        self._revDids.pop(0)
                    return True
            # nothing left in the deck; move to next
            self._revDids.pop(0)
        if self.revCount:
            # if we didn't get a card but the count is non-zero,
            # we need to check again for any cards that were
            # removed from the queue but not buried
            self._resetRev()
            return self._fillRev()

    def _getRevCard(self):
        if self._fillRev():
            self.revCount -= 1
            return self.col.getCard(self._revQueue.pop())

    def totalRevForCurrentDeck(self):
        return self.col.db.scalar(
            """
select count() from cards where id in (
select id from cards where did in %s and queue = 2 and due <= ? limit ?)"""
            % ids2str(self.col.decks.active()), self.today, self.reportLimit)

    # Answering a review card
    ##########################################################################

    def _answerRevCard(self, card, ease):
        delay = 0
        if ease == 1:
            delay = self._rescheduleLapse(card)
        else:
            self._rescheduleRev(card, ease)
        self._logRev(card, ease, delay)

    def _rescheduleLapse(self, card):
        conf = self._lapseConf(card)
        card.lastIvl = card.ivl
        if self._resched(card):
            card.lapses += 1
            card.ivl = self._nextLapseIvl(card, conf)
            card.factor = max(1300, card.factor-200)
            card.due = self.today + card.ivl
            # if it's a filtered deck, update odue as well
            if card.odid:
                card.odue = card.due
        # if suspended as a leech, nothing to do
        delay = 0
        if self._checkLeech(card, conf) and card.queue == -1:
            return delay
        # if no relearning steps, nothing to do
        if not conf['delays']:
            return delay
        # record rev due date for later
        if not card.odue:
            card.odue = card.due
        delay = self._delayForGrade(conf, 0)
        card.due = int(delay + time.time())
        card.left = self._startingLeft(card)
        # queue 1
        if card.due < self.dayCutoff:
            self.lrnCount += card.left // 1000
            card.queue = 1
            heappush(self._lrnQueue, (card.due, card.id))
        else:
            # day learn queue
            ahead = ((card.due - self.dayCutoff) // 86400) + 1
            card.due = self.today + ahead
            card.queue = 3
        return delay

    def _nextLapseIvl(self, card, conf):
        return max(conf['minInt'], int(card.ivl*conf['mult']))

    def _rescheduleRev(self, card, ease):
        # update interval
        card.lastIvl = card.ivl
        if self._resched(card):
            self._updateRevIvl(card, ease)
            # then the rest
            card.factor = max(1300, card.factor+[-150, 0, 150][ease-2])
            card.due = self.today + card.ivl
        else:
            card.due = card.odue
        if card.odid:
            card.did = card.odid
            card.odid = 0
            card.odue = 0

    def _logRev(self, card, ease, delay):
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
        "Integer interval after interval factor and prev+1 constraints applied."
        new = ivl * conf.get('ivlFct', 1)
        return int(max(new, prev+1))

    def _daysLate(self, card):
        "Number of days later than scheduled."
        due = card.odue if card.odid else card.due
        return max(0, self.today - due)

    def _updateRevIvl(self, card, ease):
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
        "Rebuild a dynamic deck."
        did = did or self.col.decks.selected()
        deck = self.col.decks.get(did)
        assert deck['dyn']
        # move any existing cards back first, then fill
        self.emptyDyn(did)
        ids = self._fillDyn(deck)
        if not ids:
            return
        # and change to our new deck
        self.col.decks.select(did)
        return ids

    def _fillDyn(self, deck):
        search, limit, order = deck['terms'][0]
        orderlimit = self._dynOrder(order, limit)
        if search.strip():
            search = "(%s)" % search
        search = "%s -is:suspended -is:buried -deck:filtered -is:learn" % search
        try:
            ids = self.col.findCards(search, order=orderlimit)
        except:
            ids = []
            return ids
        # move the cards over
        self.col.log(deck['id'], ids)
        self._moveToDyn(deck['id'], ids)
        return ids

    def emptyDyn(self, did, lim=None):
        if not lim:
            lim = "did = %s" % did
        self.col.log(self.col.db.list("select id from cards where %s" % lim))
        # move out of cram queue
        self.col.db.execute("""
update cards set did = odid, queue = (case when type = 1 then 0
else type end), type = (case when type = 1 then 0 else type end),
due = odue, odue = 0, odid = 0, usn = ? where %s""" % lim,
                            self.col.usn())

    def remFromDyn(self, cids):
        self.emptyDyn(None, "id in %s and odid" % ids2str(cids))

    def _dynOrder(self, o, l):
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
        deck = self.col.decks.get(did)
        data = []
        t = intTime(); u = self.col.usn()
        for c, id in enumerate(ids):
            # start at -100000 so that reviews are all due
            data.append((did, -100000+c, u, id))
        # due reviews stay in the review queue. careful: can't use
        # "odid or did", as sqlite converts to boolean
        queue = """
(case when type=2 and (case when odue then odue <= %d else due <= %d end)
 then 2 else 0 end)"""
        queue %= (self.today, self.today)
        self.col.db.executemany("""
update cards set
odid = (case when odid then odid else did end),
odue = (case when odue then odue else due end),
did = ?, queue = %s, due = ?, usn = ? where id = ?""" % queue, data)

    def _dynIvlBoost(self, card):
        """为临时牌组中的复习卡计算新的间隔天数。"""
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
            # handle
            a = conf['leechAction']
            if a == 0:
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
        return self.col.decks.confForDid(card.did)

    def _newConf(self, card):
        conf = self._cardConf(card)
        # normal deck
        if not card.odid:
            return conf['new']
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
        conf = self._cardConf(card)
        # normal deck
        if not card.odid:
            return conf['lapse']
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
        conf = self._cardConf(card)
        # normal deck
        if not card.odid:
            return conf['rev']
        # dynamic deck
        return self.col.decks.confForDid(card.odid)['rev']

    def _deckLimit(self):
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
        oldToday = self.today
        # days since col created
        self.today = int((time.time() - self.col.crt) // 86400)
        # end of day cutoff
        self.dayCutoff = self.col.crt + (self.today+1)*86400
        if oldToday != self.today:
            self.col.log(self.today, self.dayCutoff)
        # update all daily counts, but don't save decks to prevent needless
        # conflicts. we'll save on card answer instead
        def update(g):
            for t in "new", "rev", "lrn", "time":
                key = t+"Today"
                if g[key][0] != self.today:
                    g[key] = [self.today, 0]
        for deck in self.col.decks.all():
            update(deck)
        # unbury if the day has rolled over
        unburied = self.col.conf.get("lastUnburied", 0)
        if unburied < self.today:
            self.unburyCards()

    def _checkDay(self):
        # check if the day has rolled over
        if time.time() > self.dayCutoff:
            self.reset()

    # Deck finished state
    ##########################################################################

    def finishedMsg(self):
        return ("<b>"+_(
            "Congratulations! You have finished this deck for now.")+
            "</b><br><br>" + self._nextDueMsg())

    def _nextDueMsg(self):
        line = []
        # the new line replacements are so we don't break translations
        # in a point release
        if self.revDue():
            line.append(_("""\
Today's review limit has been reached, but there are still cards
waiting to be reviewed. For optimum memory, consider increasing
the daily limit in the options.""").replace("\n", " "))
        if self.newDue():
            line.append(_("""\
There are more new cards available, but the daily limit has been
reached. You can increase the limit in the options, but please
bear in mind that the more new cards you introduce, the higher
your short-term review workload will become.""").replace("\n", " "))
        if self.haveBuried():
            if self.haveCustomStudy:
                now = " " +  _("To see them now, click the Unbury button below.")
            else:
                now = ""
            line.append(_("""\
Some related or buried cards were delayed until a later session.""")+now)
        if self.haveCustomStudy and not self.col.decks.current()['dyn']:
            line.append(_("""\
To study outside of the normal schedule, click the Custom Study button below."""))
        return "<p>".join(line)

    def revDue(self):
        "True if there are any rev cards due."
        return self.col.db.scalar(
            ("select 1 from cards where did in %s and queue = 2 "
             "and due <= ? limit 1") % self._deckLimit(),
            self.today)

    def newDue(self):
        "True if there are any new cards due."
        return self.col.db.scalar(
            ("select 1 from cards where did in %s and queue = 0 "
             "limit 1") % self._deckLimit())

    def haveBuried(self):
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
        "Return the next interval for CARD, in seconds."
        if card.queue in (0,1,3):
            return self._nextLrnIvl(card, ease)
        elif ease == 1:
            # lapsed
            conf = self._lapseConf(card)
            if conf['delays']:
                return conf['delays'][0]*60
            return self._nextLapseIvl(card, conf)*86400
        else:
            # review
            return self._nextRevIvl(card, ease)*86400

    # this isn't easily extracted from the learn code
    def _nextLrnIvl(self, card, ease):
        if card.queue == 0:
            card.left = self._startingLeft(card)
        conf = self._lrnConf(card)
        if ease == 1:
            # fail
            return self._delayForGrade(conf, len(conf['delays']))
        elif ease == 3:
            # early removal
            if not self._resched(card):
                return 0
            return self._graduatingIvl(card, conf, True, adj=False) * 86400
        else:
            left = card.left%1000 - 1
            if left <= 0:
                # graduate
                if not self._resched(card):
                    return 0
                return self._graduatingIvl(card, conf, False, adj=False) * 86400
            else:
                return self._delayForGrade(conf, left)

    # Suspending
    ##########################################################################

    def suspendCards(self, ids):
        "Suspend cards."
        self.col.log(ids)
        self.remFromDyn(ids)
        self.removeLrn(ids)
        self.col.db.execute(
            "update cards set queue=-1,mod=?,usn=? where id in "+
            ids2str(ids), intTime(), self.col.usn())

    def unsuspendCards(self, ids):
        "Unsuspend cards."
        self.col.log(ids)
        self.col.db.execute(
            "update cards set queue=type,mod=?,usn=? "
            "where queue = -1 and id in "+ ids2str(ids),
            intTime(), self.col.usn())

    def buryCards(self, cids):
        self.col.log(cids)
        self.remFromDyn(cids)
        self.removeLrn(cids)
        self.col.db.execute("""
update cards set queue=-2,mod=?,usn=? where id in """+ids2str(cids),
                            intTime(), self.col.usn())

    def buryNote(self, nid):
        "Bury all cards for note until next session."
        cids = self.col.db.list(
            "select id from cards where nid = ? and queue >= 0", nid)
        self.buryCards(cids)

    # Sibling spacing
    ##########################################################################

    def _burySiblings(self, card):
        """
        隐藏card的兄弟卡片（同一笔记下的其它卡片）。

        详细查看文档的Siblings and Burying小节。
        :param card:卡片
        :return:无
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
        # 同时修改cards表将这些卡片在记录在“用户隐藏的”卡片队列中。

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
        "Put cards at the end of the new queue."
        self.remFromDyn(ids)
        self.col.db.execute(
            "update cards set type=0,queue=0,ivl=0,due=0,odue=0,factor=?"
            " where id in "+ids2str(ids), STARTING_FACTOR)
        pmax = self.col.db.scalar(
            "select max(due) from cards where type=0") or 0
        # takes care of mod + usn
        self.sortCards(ids, start=pmax+1)
        self.col.log(ids)

    def reschedCards(self, ids, imin, imax):
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
        "Completely reset cards for export."
        sids = ids2str(ids)
        # we want to avoid resetting due number of existing new cards on export
        nonNew = self.col.db.list(
            "select id from cards where id in %s and (queue != 0 or type != 0)"
            % sids)
        # reset all cards
        self.col.db.execute(
            "update cards set reps=0,lapses=0,odid=0,odue=0,queue=0"
            " where id in %s" % sids
        )
        # and forget any non-new cards, changing their due numbers
        self.forgetCards(nonNew)
        self.col.log(ids)

    # Repositioning new cards
    ##########################################################################

    def sortCards(self, cids, start=1, step=1, shuffle=False, shift=False):
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
        # determine nid ordering
        due = {}
        if shuffle:
            random.shuffle(nids)
        for c, nid in enumerate(nids):
            due[nid] = start+c*step
        high = start+c*step
        # shift?
        if shift:
            low = self.col.db.scalar(
                "select min(due) from cards where due >= ? and type = 0 "
                "and id not in %s" % scids,
                start)
            if low is not None:
                shiftby = high - low + 1
                self.col.db.execute("""
update cards set mod=?, usn=?, due=due+? where id not in %s
and due >= ? and queue = 0""" % scids, now, self.col.usn(), shiftby, low)
        # reorder cards
        d = []
        for id, nid in self.col.db.execute(
            "select id, nid from cards where type = 0 and id in "+scids):
            d.append(dict(now=now, due=due[nid], usn=self.col.usn(), cid=id))
        self.col.db.executemany(
            "update cards set due=:due,mod=:now,usn=:usn where id = :cid", d)

    def randomizeCards(self, did):
        cids = self.col.db.list("select id from cards where did = ?", did)
        self.sortCards(cids, shuffle=True)

    def orderCards(self, did):
        cids = self.col.db.list("select id from cards where did = ? order by id", did)
        self.sortCards(cids)

    def resortConf(self, conf):
        for did in self.col.decks.didsForConf(conf):
            if conf['new']['order'] == 0:
                self.randomizeCards(did)
            else:
                self.orderCards(did)

    # for post-import
    def maybeRandomizeDeck(self, did=None):
        if not did:
            did = self.col.decks.selected()
        conf = self.col.decks.confForDid(did)
        # in order due?
        if conf['new']['order'] == NEW_CARDS_RANDOM:
            self.randomizeCards(did)
