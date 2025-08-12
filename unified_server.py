"""
Unified FastAPI Server - Čistý a funkční server pro Academic Research Tool
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import prometheus_client
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
from fastapi import Response
from cache_manager import CacheManager

# Import unified components
try:
    from unified_config import get_config
    from academic_scraper import create_scraping_orchestrator, ScrapingResult
    UNIFIED_CONFIG_AVAILABLE = True
    print("✅ Unified server using unified configuration")
except ImportError as e:
    UNIFIED_CONFIG_AVAILABLE = False
    print(f"⚠️  Unified config unavailable: {e}")

# Globální metriky (singletony)
scrape_counter = Counter('scrape_requests_total', 'Počet scraping požadavků', ['source'], registry=REGISTRY)
scrape_duration = Histogram('scrape_duration_seconds', 'Doba trvání scraping požadavků', ['source'], registry=REGISTRY)
cache_hits = Counter('cache_hits_total', 'Počet cache hitů', registry=REGISTRY)
cache_misses = Counter('cache_misses_total', 'Počet cache missů', registry=REGISTRY)

# Pydantic models pro API
class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Search query")
    sources: Optional[List[str]] = Field(
        default=None,
        description="List of sources to scrape (wikipedia, pubmed). If None, all sources will be used."
    )

class ScrapeResponse(BaseModel):
    success: bool
    query: str
    results: List[Dict[str, Any]]
    total_sources: int
    successful_sources: int
    execution_time: float

class HealthResponse(BaseModel):
    status: str
    uptime: float
    version: str
    environment: str
    scrapers_available: List[str]

class SourcesResponse(BaseModel):
    available_sources: List[str]
    source_configs: Dict[str, Dict[str, Any]]

class UnifiedServer:
    """Unified FastAPI server pro Academic Research Tool"""

    def __init__(self):
        self.app = FastAPI(
            title="Academic Research Tool API",
            description="Unified API for academic content scraping and research",
            version="2.0.0",
            docs_url="/docs",
            redoc_url="/redoc"
        )

        # Load configuration
        if UNIFIED_CONFIG_AVAILABLE:
            self.config = get_config()
            cors_enabled = self.config.api.cors_enabled
        else:
            cors_enabled = True

        # Setup CORS
        if cors_enabled:
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        # Initialize cache manager (shared cache adapter)
        self.cache = CacheManager()

        # Setup routes
        self._setup_routes()

        # Startup time tracking
        self.startup_time = time.time()

        # Setup logging
        self.logger = logging.getLogger(__name__)

    def _setup_routes(self):
        """Setup all API routes"""

        @self.app.get("/", response_class=HTMLResponse)
        async def root():
            """Landing page with API documentation links"""
            return """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Academic Research Tool API</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 40px; }
                    h1 { color: #2c3e50; }
                    ul { line-height: 1.6; }
                    a { color: #3498db; text-decoration: none; }
                    a:hover { text-decoration: underline; }
                    .status { background: #ecf0f1; padding: 20px; border-radius: 5px; }
                </style>
            </head>
            <body>
                <h1>🔬 Academic Research Tool API</h1>
                <p>Unified API for academic content scraping and research.</p>
                <h2>📚 Documentation</h2>
                <ul>
                    <li><a href="/docs">Interactive API Documentation (Swagger UI)</a></li>
                    <li><a href="/redoc">Alternative Documentation (ReDoc)</a></li>
                </ul>
                <h2>🔗 API Endpoints</h2>
                <ul>
                    <li><a href="/health">💚 Health Check</a></li>
                    <li><a href="/api/v1/sources">📋 Available Sources</a></li>
                    <li>🔍 POST /api/v1/scrape - Main scraping endpoint</li>
                </ul>
                <div class="status">
                    <p><em>Status: ✅ Server is running | Powered by FastAPI + Academic Scraper</em></p>
                </div>
            </body>
            </html>
            """

        @self.app.get("/health", response_model=HealthResponse)
        async def health_check():
            """Health check endpoint"""
            try:
                # Test orchestrator availability
                orchestrator = create_scraping_orchestrator()
                available_scrapers = list(orchestrator.scrapers.keys())

                uptime = time.time() - self.startup_time

                return HealthResponse(
                    status="healthy",
                    uptime=uptime,
                    version="2.0.0",
                    environment=self.config.environment.value if UNIFIED_CONFIG_AVAILABLE else "unknown",
                    scrapers_available=available_scrapers
                )

            except Exception as e:
                self.logger.error(f"Health check failed: {e}")
                raise HTTPException(status_code=503, detail=f"Service unhealthy: {str(e)}")

        @self.app.get("/api/v1/sources", response_model=SourcesResponse)
        async def get_sources():
            """Get available sources and their configurations"""
            try:
                orchestrator = create_scraping_orchestrator()
                available_sources = list(orchestrator.scrapers.keys())

                source_configs = {}
                if UNIFIED_CONFIG_AVAILABLE:
                    for source in available_sources:
                        source_config = self.config.get_source_config(source)
                        if source_config:
                            source_configs[source] = {
                                "name": source_config.name,
                                "enabled": source_config.enabled,
                                "rate_limit_delay": source_config.rate_limit_delay,
                                "base_url": source_config.base_url
                            }

                return SourcesResponse(
                    available_sources=available_sources,
                    source_configs=source_configs
                )

            except Exception as e:
                self.logger.error(f"Failed to get sources: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to retrieve sources: {str(e)}")

        @self.app.get("/metrics")
        async def metrics():
            """Prometheus metrics endpoint"""
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        @self.app.post("/api/v1/scrape", response_model=ScrapeResponse)
        async def scrape_sources(request: ScrapeRequest):
            """Main scraping endpoint"""
            start_time = time.time()
            cache_key = f"{request.query.lower().strip()}|{','.join(request.sources or [])}"
            cached = self.cache.get(cache_key)
            if cached:
                cache_hits.inc()
                self.logger.info(f"Cache hit for {cache_key}")
                return cached
            cache_misses.inc()
            try:
                self.logger.info(f"Scraping request: query='{request.query}', sources={request.sources}")

                # Create orchestrator and perform scraping
                orchestrator = create_scraping_orchestrator()
                results = await orchestrator.scrape_all_sources(request.query, request.sources)

                # Process results
                successful_results = [r for r in results if r.success]
                execution_time = time.time() - start_time

                # Convert ScrapingResult objects to dictionaries
                results_data = []
                for result in results:
                    scrape_counter.labels(result.source).inc()
                    if result.response_time is not None:
                        scrape_duration.labels(result.source).observe(result.response_time)

                    result_dict = {
                        "source": result.source,
                        "success": result.success,
                        "data": result.data,
                        "error": result.error,
                        "execution_time": result.response_time,  # Použij response_time místo execution_time
                        "cached": False
                    }
                    results_data.append(result_dict)

                response = ScrapeResponse(
                    success=len(successful_results) > 0,
                    query=request.query,
                    results=results_data,
                    total_sources=len(results),
                    successful_sources=len(successful_results),
                    execution_time=execution_time
                )
                self.cache.set(cache_key, response)
                self.logger.info(f"Scraping completed: {len(successful_results)}/{len(results)} sources successful")

                return response

            except Exception as e:
                self.logger.error(f"Scraping failed: {e}")
                execution_time = time.time() - start_time

                response = ScrapeResponse(
                    success=False,
                    query=request.query,
                    results=[],
                    total_sources=0,
                    successful_sources=0,
                    execution_time=execution_time
                )
                return response

        # Unified Research endpoint using UnifiedResearchEngine
        @self.app.post("/api/v1/research")
        async def research_endpoint(payload: Dict[str, Any]):
            try:
                from unified_research_engine import UnifiedResearchEngine, ResearchRequest
                engine = UnifiedResearchEngine()
                req = ResearchRequest(
                    query=payload.get('query', ''),
                    strategy=payload.get('strategy', 'balanced'),
                    domain=payload.get('domain', 'general'),
                    sources=payload.get('sources'),
                    max_results=payload.get('max_results', 10),
                    user_id=payload.get('user_id', 'default'),
                    budget_limit=payload.get('budget_limit')
                )
                result = await engine.research(req)
                return {
                    'query_id': result.query_id,
                    'success': result.success,
                    'sources_found': result.sources_found,
                    'total_tokens': result.total_tokens,
                    'cost': result.cost,
                    'execution_time': result.execution_time,
                    'summary': result.summary,
                    'key_findings': result.key_findings,
                    'cached': result.cached,
                    'timestamp': result.timestamp.isoformat(),
                    'detailed_results': result.detailed_results,
                }
            except Exception as e:
                self.logger.error(f"Research endpoint failed: {e}")
                raise HTTPException(status_code=500, detail=str(e))

    def get_app(self) -> FastAPI:
        """Get FastAPI application instance"""
        return self.app

# Create server instance
def create_app() -> FastAPI:
    """Create and configure FastAPI application"""
    server = UnifiedServer()
    return server.get_app()

# For direct running
if __name__ == "__main__":
    import uvicorn

    app = create_app()

    # Load config for port
    try:
        config = get_config()
        port = config.api.port
        host = config.api.host
    except:
        port = 8000
        host = "0.0.0.0"

    print(f"🚀 Starting Academic Research Tool API on {host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=True)
