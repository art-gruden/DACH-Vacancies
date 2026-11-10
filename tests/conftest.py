import os

import pytest

from vacancies_bot.crawler.client import CrawlerClient
from vacancies_bot.db.session import Database
from vacancies_bot.domain.criteria import LanguageRef, RoleRef, SalaryPref, SearchCriteria, SkillRef
from vacancies_bot.services.facets import FacetsService
from vacancies_bot.testing.fake_crawler import TOKEN, FakeCrawler

PG_URL = os.environ.get("BOT_TEST_PG_URL")  # e.g. postgresql+asyncpg://postgres@/bot_test?host=/tmp/pg&port=5433


async def _noop_sleep(_):
    return None


@pytest.fixture(params=["sqlite", "pg"] if PG_URL else ["sqlite"])
async def db(request, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/bot.db" if request.param == "sqlite" else PG_URL
    d = Database(url)
    await d.drop_all()
    await d.create_all()
    yield d
    await d.drop_all()
    await d.dispose()


@pytest.fixture
def fake():
    return FakeCrawler()


@pytest.fixture
async def client(fake):
    c = CrawlerClient("https://crawler.test/api/bot/v1", TOKEN, transport=fake.transport, sleep=_noop_sleep)
    yield c
    await c.aclose()


@pytest.fixture
def facets(client, db):
    return FacetsService(client, db, ttl_s=60)


def devops_criteria(**over) -> SearchCriteria:
    base = dict(
        roles=[RoleRef(name="DevOps / SRE", slug="devops-sre", id=10, group="information-technology")],
        experience="6-10",
        skills=[
            SkillRef(name="Kubernetes", proficiency="advanced", core=True),
            SkillRef(name="Terraform", proficiency="advanced", core=True),
            SkillRef(name="Ansible", proficiency="working"),
            SkillRef(name="GitLab CI", proficiency="working"),
        ],
        countries=["DE", "CH"],
        work_models=["REMOTE", "HYBRID"],
        remote_from="DE",
        employment_types=["FULL_TIME", "CONTRACT"],
        languages=[LanguageRef(code="de", level="B2"), LanguageRef(code="en", level="C1")],
        language_flexibility="WARN",
        salary=SalaryPref(amount=75000, currency="EUR", strict=False),
        exclusions=["Werkstudent"],
        search_breadth="BALANCED",
        min_score=70,
    )
    base.update(over)
    return SearchCriteria(**base)


@pytest.fixture
def criteria():
    return devops_criteria()
