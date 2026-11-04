import threading
import time

from collector.crawler import HostGovernor

O = "https://www.senate.go.th"


def gov(**kw):
    opts = dict(delay=1.0, max_parallel=2, max_delay=8.0, user_agent="test",
                stop=threading.Event(), trip_after=100)
    opts.update(kw)
    return HostGovernor(**opts)


def test_overload_doubles_spacing_up_to_cap_then_recovers():
    g = gov()
    g.record(O, "overload", detail="HTTP 503")
    assert g.hosts[O].delay == 1.0  # one failure on its own could just be a broken URL
    g.record(O, "overload", detail="HTTP 503")
    assert g.hosts[O].delay == 2.0  # two in a row: the server is struggling
    for _ in range(5):
        g.record(O, "overload")
    assert g.hosts[O].delay == 8.0  # capped at max_delay
    g.record(O, "ok")
    assert g.hosts[O].delay == 6.4  # recovers gradually, not all at once
    for _ in range(30):
        g.record(O, "ok")
    assert g.hosts[O].delay == 1.0  # never below the normal spacing


def test_isolated_overload_between_successes_does_not_slow_down():
    g = gov()
    for _ in range(10):
        g.record(O, "overload", detail="HTTP 503")
        g.record(O, "ok")
    assert g.hosts[O].delay == 1.0


def test_broken_pages_never_slow_down():
    # What got the real crawl stuck at 60s: runs of broken pages (HTTP 500), each run cut
    # short by one success just before the health check would have reset the spacing.
    g = gov(trip_after=5)
    g.session.get = lambda url, timeout: _Reply(200)
    for _ in range(10):
        for _ in range(4):
            g.record(O, "failed", detail="HTTP 500")
        g.record(O, "ok")
    st = g.hosts[O]
    assert st.delay == 1.0 and st.paused_until == 0


class _Reply:
    def __init__(self, status_code):
        self.status_code = status_code


def test_healthy_server_check_resets_spacing():
    g = gov(trip_after=5)
    g.session.get = lambda url, timeout: _Reply(200)  # robots.txt answers normally
    for _ in range(5):
        g.record(O, "overload", detail="HTTP 503")
    st = g.hosts[O]
    assert st.delay == 1.0 and st.failures == 0 and O not in g.dead


def test_healthy_check_with_nothing_else_working_pauses():
    g = gov(trip_after=5, ok_window=0.2)
    g.session.get = lambda url, timeout: _Reply(200)  # robots.txt is fine...
    for _ in range(5):
        g.record(O, "failed", detail="HTTP 403")
    assert g.hosts[O].paused_until == 0  # (just started: give it the benefit of the doubt)
    time.sleep(0.3)
    for _ in range(5):  # ...but nothing else has worked for a while
        g.record(O, "failed", detail="HTTP 403")
    assert g.hosts[O].paused_until > time.monotonic() + 200


def test_burst_of_broken_links_does_not_pause():
    # What paused the real crawl for 5 minutes: with 6 connections open, a batch of broken links
    # failed 10 times in a row within two seconds, two health checks with no success in between.
    g = gov(trip_after=5)
    g.session.get = lambda url, timeout: _Reply(200)
    g.record(O, "ok")
    for _ in range(10):
        g.record(O, "failed", detail="HTTP 500")
    assert g.hosts[O].paused_until == 0


def test_failing_server_check_keeps_spacing_and_pauses():
    g = gov(trip_after=5)
    g.session.get = lambda url, timeout: _Reply(503)
    for _ in range(5):
        g.record(O, "overload", detail="HTTP 503")
    st = g.hosts[O]
    assert st.delay == 8.0 and st.paused_until > time.monotonic() + 200


def test_failed_counts_toward_block_detection_without_slowing():
    g = gov()
    g.record(O, "failed", detail="HTTP 403")
    assert g.hosts[O].delay == 1.0
    assert g.hosts[O].failures == 1
    g.record(O, "ok")
    assert g.hosts[O].failures == 0


def test_retry_after_is_obeyed():
    g = gov()
    g.record(O, "overload", retry_after=30)
    assert g.hosts[O].next_slot >= time.monotonic() + 29


def test_requests_are_spaced_per_host():
    g = gov(delay=0.2)
    t0 = time.monotonic()
    for _ in range(3):
        g.wait_turn(O)
    assert time.monotonic() - t0 >= 0.39  # 3 requests -> 2 gaps of 0.2s
    t1 = time.monotonic()
    g.wait_turn("https://document.senate.go.th")  # other hosts are not held up
    assert time.monotonic() - t1 < 0.1


def test_page_lane_is_separate_from_downloads():
    g = gov(max_parallel=1)
    got_page = threading.Event()
    with g.connection(O, "file"):  # the only download slot is busy...
        def page():
            with g.connection(O, "page"):
                got_page.set()
        threading.Thread(target=page).start()
        assert got_page.wait(1)  # ...but a page still gets through
        second_file = threading.Event()
        def file():
            with g.connection(O, "file"):
                second_file.set()
        threading.Thread(target=file).start()
        assert not second_file.wait(0.3)  # while another download has to wait
    assert second_file.wait(1)


def test_page_slots_are_configurable():
    g = gov(page_parallel=2)
    with g.connection(O, "page"), g.connection(O, "page"):
        third = threading.Event()
        def page():
            with g.connection(O, "page"):
                third.set()
        threading.Thread(target=page).start()
        assert not third.wait(0.3)
    assert third.wait(1)


def test_crawl_delay_from_robots_raises_the_floor():
    g = gov(delay=0.1)
    g.wait_turn(O, floor=10)
    assert g.hosts[O].base == 10 and g.hosts[O].delay == 10
