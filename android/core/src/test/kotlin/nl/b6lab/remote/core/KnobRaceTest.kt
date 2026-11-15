package nl.b6lab.remote.core

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Fast knob drags against a pedal that applies knob changes late ([FakePedal] `knobLag`), like the real B6: the
 * confirming read after a drag may show an older value. The app must end on the value the user set (and the
 * pedal must have it), never show stale values of other knobs, and when the pedal really keeps another value,
 * show that value with an error.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class KnobRaceTest {
    private val records = Json.parseToJsonElement(javaClass.getResource("/fixtures/ptcf.json")!!.readText())
        .jsonArray.map { it.jsonObject }
    private val catalog = Catalog.load(javaClass.getResource("/fixtures/catalog.json")!!.readText())

    private fun hex(s: String) = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    private fun image(slot: Int) = hex(records.first { it["slot"]!!.jsonPrimitive.int == slot }["raw"]!!.jsonPrimitive.content)

    private class Rig(val p: FakePedal, val s: B6Session, val c: RemoteController)

    private suspend fun TestScope.rig(busy: Boolean, lag: Int, patches: List<ByteArray> = List(240) { image(it) }): Rig {
        val p = FakePedal(patches, busy = busy, knobLag = lag)
        val s = B6Session(backgroundScope, p)
        s.connect()
        val c = RemoteController(backgroundScope, s, catalog)
        runCurrent()
        advanceTimeBy(20_000) // the search index is read before the test starts
        runCurrent()
        return Rig(p, s, c)
    }

    private fun TestScope.settle() {
        advanceTimeBy(1000)
        runCurrent()
    }

    /** A shown pedal whose entry is its own position, with three knobs reaching at least [max]; it gets selected. */
    private fun TestScope.pick(r: Rig, max: Int): Pair<Int, List<Int>> {
        val edit = r.s.state.value.edit!!
        for (pedal in r.c.ui.value.pedals) {
            if (Ptcf.position(edit, pedal.slot) != pedal.slot) continue
            r.c.tapPedal(pedal.slot)
            runCurrent()
            val knobs = r.c.ui.value.knobs.filter { it.max >= max }.map { it.index }
            if (knobs.size >= 3) return pedal.slot to knobs
        }
        error("no pedal with three knobs up to $max")
    }

    /** [image] with every editable knob of every entry at 7 (or its field maximum): same effects, other values. */
    private fun twinOf(image: ByteArray): ByteArray {
        var p = Ptcf.parse(image)
        for (e in p.effects) for (i in e.params.indices) if (e.editable(i)) p = Ptcf.setParam(p, e.slot, i, minOf(7, e.maxValue(i)))
        return Ptcf.encode(p)
    }

    /**
     * Without PC mode the session polls every [B6Session.POLL_MS] from connect: start just after a poll, so the
     * drag, the patch change and the confirm all fall before the next one (which would load the new patch first).
     */
    private fun TestScope.afterPoll() {
        advanceTimeBy(B6Session.POLL_MS - currentTime % B6Session.POLL_MS + 10)
        runCurrent()
    }

    private fun span(p: Patch, slot: Int) = if (slot + 1 < p.effects.size && p.effects[slot + 1].isTail) 2 else 1

    private fun shown(r: Rig, index: Int) = r.c.ui.value.knobs.first { it.index == index }.value
    private fun onPedal(r: Rig, entry: Int, index: Int) = Ptcf.parse(r.p.editBuffer()).effects[entry].params[index]

    /** Every value of knob [index] the board shows from now on. */
    private fun TestScope.record(r: Rig, index: Int): List<Int> {
        val seen = ArrayList<Int>()
        backgroundScope.launch { r.c.ui.collect { ui -> ui.knobs.firstOrNull { it.index == index }?.let { seen.add(it.value) } } }
        runCurrent()
        return seen
    }

    /** Drags knob [index] through [values], one step every [stepMs] (a fast finger). */
    private fun TestScope.drag(r: Rig, index: Int, values: IntProgression, stepMs: Long = 4) {
        for (v in values) {
            r.c.dragKnob(index, v)
            advanceTimeBy(stepMs)
            runCurrent()
        }
    }

    @Test
    fun fastDragEndsOnTheValueSet() = runTest {
        for (busy in listOf(false, true)) {
            val r = rig(busy, lag = 2)
            val (entry, knobs) = pick(r, 60)
            val k = knobs[0]
            // ZNR THRSH 25 -> 50 in the report; another range when the pedal already holds a value in it
            val (from, to) = if (onPedal(r, entry, k) in 25..50) 0 to 20 else 25 to 50
            r.c.dragKnob(k, from)
            settle()
            assertEquals("busy=$busy start", from, shown(r, k))
            val seen = record(r, k)
            drag(r, k, (from + 1)..to)
            settle()
            settle()
            assertEquals("busy=$busy shown", to, shown(r, k))
            assertEquals("busy=$busy pedal", to, onPedal(r, entry, k))
            assertNull(r.c.ui.value.error)
            // once at the end value the board never jumps back
            val after = seen.dropWhile { it != to }
            assertTrue("busy=$busy shown $seen", after.isNotEmpty() && after.all { it == to })
        }
    }

    @Test
    fun dragNeverShowsStaleValuesOfOtherKnobs() = runTest {
        for (busy in listOf(false, true)) {
            val r = rig(busy, lag = 4)
            val (entry, knobs) = pick(r, 40)
            val (a, b, c) = knobs
            r.c.dragKnob(a, 10)
            r.c.dragKnob(b, 10)
            settle()
            val seenB = record(r, b)
            // B fast to 20, then at once one step of A: B's last values are still queued on the pedal
            drag(r, b, 11..20)
            r.c.dragKnob(a, 30)
            advanceTimeBy(40)
            runCurrent()
            settle()
            settle()
            assertEquals("busy=$busy", 30, shown(r, a))
            assertEquals("busy=$busy", 20, shown(r, b))
            assertEquals(20, onPedal(r, entry, b))
            val after = seenB.dropWhile { it != 20 }
            assertTrue("busy=$busy B shown $seenB", after.isNotEmpty() && after.all { it == 20 })
            // a knob the app did not touch follows the pedal once it settles
            val cv = (shown(r, c) + 7) % 40
            r.p.turnKnob(entry, c, cv)
            settle()
            assertEquals("busy=$busy", cv, shown(r, c))
            assertNull(r.c.ui.value.error)
        }
    }

    @Test
    fun refusedKnobShowsThePedalsValueWithAnError() = runTest {
        for (busy in listOf(false, true)) {
            val r = rig(busy, lag = 2)
            val (entry, knobs) = pick(r, 60)
            val k = knobs[0]
            val kept = onPedal(r, entry, k)
            val want = if (kept == 50) 40 else 50
            r.p.pinKnob(entry, k)
            drag(r, k, (want - 5)..want)
            settle()
            settle()
            assertEquals("busy=$busy", kept, shown(r, k))
            assertEquals(kept, onPedal(r, entry, k))
            assertNotNull("busy=$busy", r.c.ui.value.error)
            // the last value is sent again a bounded number of times
            val last = r.p.sent.count { it.contentEquals(byteArrayOf(0xF0.toByte()) + Msg.setParam(entry, k, want)[1] + byteArrayOf(0xF7.toByte())) }
            assertTrue("busy=$busy sent $last times", last in 1..4)
        }
    }

    @Test
    fun patchChangeOnThePedalDuringConfirmWritesNothing() = runTest {
        for (busy in listOf(false, true)) {
            // slot 1 has the same effects as slot 0, all knobs at 7
            val r = rig(busy, lag = 2, patches = List(240) { if (it == 1) twinOf(image(0)) else image(it) })
            val (entry, knobs) = pick(r, 60)
            val k = knobs[0]
            afterPoll()
            drag(r, k, 30..45)
            advanceTimeBy(40) // the last throttled value is out, the confirm (150 ms later) has not run
            runCurrent()
            r.p.footswitchTo(1)
            settle()
            settle()
            assertEquals("busy=$busy", 1, r.s.state.value.slot)
            assertEquals("busy=$busy pedal", 7, onPedal(r, entry, k))
            assertTrue("busy=$busy nothing written", Ptcf.samePatch(r.p.storedSlot(1), r.p.editBuffer()))
            assertEquals("busy=$busy shown", 7, r.s.state.value.edit!!.effects[entry].params[k])
            assertFalse("busy=$busy", r.s.state.value.dirty)
        }
    }

    @Test
    fun deleteRightAfterDragKeepsTheValueOnTheDraggedEffect() = runTest {
        for (busy in listOf(false, true)) {
            for (lag in listOf(2, 4)) {
                val r = rig(busy, lag)
                val (entry, knobs) = pick(r, 60)
                val k = knobs[0]
                val before = r.s.state.value.edit!!
                // another pedal, before the dragged one when there is one: the dragged effect moves down
                val other = r.c.ui.value.pedals.map { it.slot }.let { s -> s.firstOrNull { it < entry } ?: s.first { it > entry } }
                val moved = if (other < entry) entry - span(before, other) else entry
                val want = if (before.effects[entry].params[k] == 44) 33 else 44
                drag(r, k, (want - 6)..want)
                advanceTimeBy(40) // the last value is sent, not yet confirmed (150 ms)
                runCurrent()
                r.c.remove(other)
                settle()
                settle()
                val pedal = Ptcf.parse(r.p.editBuffer())
                assertEquals("busy=$busy lag=$lag", before.effects[entry].fxId, pedal.effects[moved].fxId)
                assertEquals("busy=$busy lag=$lag pedal", want, pedal.effects[moved].params[k])
                assertEquals("busy=$busy lag=$lag shown", want, r.s.state.value.edit!!.effects[moved].params[k])
                assertTrue(Ptcf.samePatch(r.s.state.value.edit!!.raw, r.p.editBuffer()))
                assertNull("busy=$busy lag=$lag", r.s.state.value.error)
            }
        }
    }

    @Test
    fun saveRightAfterDragStoresTheValue() = runTest {
        for (busy in listOf(false, true)) {
            for (lag in listOf(2, 4)) {
                val r = rig(busy, lag)
                val (entry, knobs) = pick(r, 60)
                val k = knobs[0]
                val want = if (onPedal(r, entry, k) == 44) 33 else 44
                drag(r, k, (want - 6)..want)
                advanceTimeBy(40) // the last value is sent, not yet confirmed (150 ms)
                runCurrent()
                r.c.save()
                settle()
                settle()
                assertNull("busy=$busy lag=$lag", r.s.state.value.error)
                assertEquals("busy=$busy lag=$lag stored", want, Ptcf.parse(r.p.storedSlot(0)).effects[entry].params[k])
                assertEquals("busy=$busy lag=$lag", want, r.s.state.value.edit!!.effects[entry].params[k])
                assertFalse("busy=$busy lag=$lag", r.s.state.value.dirty)
            }
        }
    }

    @Test
    fun patchArrowRightAfterDragLeavesTheNextPatchAlone() = runTest {
        for (busy in listOf(false, true)) {
            val r = rig(busy, lag = 4, patches = List(240) { if (it == 1) twinOf(image(0)) else image(it) })
            val (entry, knobs) = pick(r, 60)
            val k = knobs[0]
            afterPoll()
            drag(r, k, 30..45)
            advanceTimeBy(40)
            runCurrent()
            r.c.nextPatch()
            settle()
            settle()
            assertEquals("busy=$busy", 1, r.s.state.value.slot)
            assertEquals("busy=$busy pedal", 7, onPedal(r, entry, k))
            assertTrue("busy=$busy nothing written", Ptcf.samePatch(r.p.storedSlot(1), r.p.editBuffer()))
            assertFalse("busy=$busy", r.s.state.value.dirty)
        }
    }
}
