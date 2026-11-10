from conftest import devops_criteria
from vacancies_bot.services.search import SearchService


async def test_initial_search_end_to_end(client, fake, criteria):
    svc = SearchService(client, per_query=50)
    out = await svc.search(criteria)
    ids = [r.job.id for r in out.ranked]
    assert set(ids) == {1, 2, 6, 9, 10, 14}
    assert ids[0] in (1, 2, 10)  # direct DevOps/SRE matches first, adjacent (9, 14) and unknown (6) later
    assert out.ranked[-1].job.id in (6, 9, 14)
    assert len(out.queries) == 4 and fake.requests["jobs"] == 4
    assert all(r.match.passed for r in out.ranked)


async def test_strict_search_is_narrower(client):
    out = await SearchService(client).search(devops_criteria(search_breadth="STRICT"))
    assert {r.job.id for r in out.ranked} == {1, 2, 6, 10}


async def test_unknown_skill_part_does_not_break_search(client, fake):
    from vacancies_bot.domain.criteria import SkillRef
    c = devops_criteria(skills=[SkillRef(name="Kubernetes", core=True), SkillRef(name="Kubernetes2000", core=True, canonical=True)])
    out = await SearchService(client).search(c)
    assert out.failed_queries and out.ranked  # the skills query got 422, the specialization query still worked


async def test_reranker_cannot_bypass_hard_filters(client, criteria):
    class Boost:
        async def similarities(self, user_id, c, jobs):
            return {j.id: 1.0 for j in jobs} | {3: 1.0, 4: 1.0}

    out = await SearchService(client, reranker=Boost(), semantic_weight=0.5).search(criteria)
    assert {r.job.id for r in out.ranked} == {1, 2, 6, 9, 10, 14}
    assert all(r.match.semantic == 1.0 for r in out.ranked)
