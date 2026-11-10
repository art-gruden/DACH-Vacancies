from __future__ import annotations

from dataclasses import dataclass

from ..ai.cv import CvStore
from ..ai.explain import Explainer
from ..ai.profile_parser import ProfileParser
from ..ai.provider import AIGateway
from ..ai.search_assistant import SearchAssistant
from ..ai.semantic import SemanticReranker
from ..config import Settings
from ..crawler.client import CrawlerClient
from ..db.session import Database
from ..feed.ingest import FeedIngestor
from ..feed.processor import FeedProcessor
from ..questionnaire.engine import Engine
from ..services.facets import FacetsService
from ..services.feedback import FeedbackService
from ..services.search import SearchService
from ..services.subscriptions import SubscriptionService
from ..services.users import UserService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    client: CrawlerClient
    facets: FacetsService
    users: UserService
    search: SearchService
    ingestor: FeedIngestor
    processor: FeedProcessor
    subs: SubscriptionService
    feedback: FeedbackService
    engine: Engine
    gateway: AIGateway
    parser: ProfileParser
    assistant: SearchAssistant
    explainer: Explainer | None
    reranker: SemanticReranker | None
    cv: CvStore


def build_context(settings: Settings, *, db: Database | None = None, client: CrawlerClient | None = None, ai_provider=None) -> AppContext:
    from ..ai.provider import OpenAICompatProvider
    from .texts import t

    db = db or Database(settings.database_url)
    client = client or CrawlerClient(settings.crawler_base_url, settings.crawler_api_token.get_secret_value(),
                                     timeout=settings.crawler_timeout_s, max_retries=settings.crawler_max_retries)
    facets = FacetsService(client, db, settings.facets_ttl_s)
    provider = ai_provider
    if provider is None and settings.ai_provider == "openai_compat":
        provider = OpenAICompatProvider(settings.ai_base_url, settings.ai_api_key.get_secret_value(), settings.ai_chat_model,
                                        settings.ai_embed_model, timeout=settings.ai_timeout_s, extra_body=settings.ai_chat_extra)
    gateway = AIGateway(db, provider, label=settings.ai_provider_label, monthly_budget_usd=settings.ai_monthly_budget_usd,
                        daily_user_calls=settings.ai_daily_user_calls, price_in_per_1k=settings.ai_price_in_per_1k,
                        price_out_per_1k=settings.ai_price_out_per_1k)
    reranker = SemanticReranker(db, gateway) if gateway.can_embed else None
    search = SearchService(client, per_query=min(settings.candidates_per_query, 100), reranker=reranker,
                           semantic_weight=settings.ai_semantic_weight)
    ingestor = FeedIngestor(db, client, page_size=settings.feed_page_size)
    processor = FeedProcessor(db, client, max_attempts=settings.event_max_attempts, morning_hour=settings.morning_digest_hour,
                              evening_hour=settings.evening_digest_hour, retention_days=settings.inbox_retention_days)
    subs = SubscriptionService(db, search, ingestor, processor, catchup_budget_s=settings.feed_catchup_budget_s)
    parser = ProfileParser(facets, gateway)
    return AppContext(
        settings=settings, db=db, client=client, facets=facets, users=UserService(db, settings.default_language), search=search,
        ingestor=ingestor, processor=processor, subs=subs, feedback=FeedbackService(db), engine=Engine(facets, t), gateway=gateway,
        parser=parser, assistant=SearchAssistant(parser), explainer=Explainer(gateway) if gateway.available else None, reranker=reranker,
        cv=CvStore(db, settings.cv_encryption_key.get_secret_value(), settings.cv_retention_hours, settings.cv_max_bytes),
    )
