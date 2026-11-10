import pytest

from vacancies_bot.crawler.client import (
    CrawlerAuthError,
    CrawlerClient,
    CrawlerDisabled,
    CrawlerUnavailable,
    CrawlerValidationError,
    CursorAheadError,
    InvalidCursorError,
    JobQuery,
)
from vacancies_bot.testing.fake_crawler import TOKEN, FakeCrawler


async def _noop(_):
    return None


def client(fake: FakeCrawler, token: str = TOKEN) -> CrawlerClient:
    return CrawlerClient("https://crawler.test/api/bot/v1", token, transport=fake.transport, sleep=_noop)


async def test_search_and_pagination():
    fake = FakeCrawler()
    c = client(fake)
    page = await c.search_jobs(JobQuery(country=["DE"]), limit=3)
    assert len(page.items) == 3 and page.has_more and page.next_cursor
    ids = [j.id async for j in c.iter_jobs(JobQuery(country=["DE"]), max_items=100, page_size=3)]
    assert sorted(ids) == [2, 3, 4, 7, 8, 9, 14]  # DE open jobs in the fixtures, no repeats
    assert all(fake.jobs[i]["status"] == "open" for i in ids)


async def test_detail_and_404():
    c = client(FakeCrawler())
    job = await c.get_job(1)
    assert job and job.description and job.salary.currency == "CHF"
    assert await c.get_job(999) is None


async def test_facets():
    c = client(FakeCrawler())
    assert {x.code for x in await c.countries()} == {"DE", "AT", "CH"}
    assert any(r.code == "DE-SN" and r.open_jobs >= 2 for r in await c.regions("DE"))
    assert [x.city for x in await c.cities("DE", q="dre")] == ["Dresden"]
    tree = await c.specializations()
    assert tree[0].children and tree[0].slug == "information-technology"
    assert (await c.skills(q="kub"))[0].name == "Kubernetes"


async def test_auth_and_disabled_errors():
    fake = FakeCrawler()
    with pytest.raises(CrawlerAuthError):
        await client(fake, token="wrong").countries()
    fake.disabled = True
    with pytest.raises(CrawlerDisabled):
        await client(fake).countries()


async def test_validation_error_not_retried():
    fake = FakeCrawler()
    c = client(fake)
    with pytest.raises(CrawlerValidationError):
        await c.search_jobs(JobQuery(skills=["NoSuchSkill"]))
    assert c.calls == 1


async def test_rate_limit_retry_and_unavailable():
    fake = FakeCrawler()
    fake.rate_limit_next = 2
    c = client(fake)
    assert await c.countries()
    assert c.calls == 3
    fake.down = True
    with pytest.raises(CrawlerUnavailable):
        await c.countries()


async def test_changes_feed_errors():
    fake = FakeCrawler()
    fake.emit("job.created", 1)
    c = client(fake)
    head = await c.changes_head()
    page = await c.changes(None)
    assert [e.event_id for e in page.items] == [1] and page.next_cursor == head
    empty = await c.changes(head)
    assert empty.items == [] and empty.next_cursor == head
    with pytest.raises(InvalidCursorError):
        await c.changes("not-base64!")
    fake.events.clear()  # crawler restored from an older backup
    with pytest.raises(CursorAheadError):
        await c.changes(head)
