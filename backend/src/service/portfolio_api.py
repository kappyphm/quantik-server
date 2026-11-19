"""Market reads and server-side portfolios for Portfolio Intelligence.

Market endpoints serve only already-published data: the latest published
screening run supplies per-symbol prices, its manifest supplies candle
history, and the run's universe supplies exchange and sector. Screening
membership itself stays on the existing /api/v1/screenings endpoints.
Company names are not part of the certified universe payload, so `name`
is null until universe ingestion retains them.
"""
import time
from collections.abc import Callable

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select

from service import jobs
from service.models import (DataManifest, HistoryRevision, Portfolio,
    PortfolioPosition, PublishedScreening, ScreeningResult, ScreeningRun,
    UniverseVersion)


class PositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    quantity: int = Field(gt=0, le=1_000_000_000)
    cost_vnd: float = Field(ge=0)


class PortfolioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cash_vnd: float = Field(ge=0)
    positions: list[PositionInput] = Field(max_length=200)

    @model_validator(mode="after")
    def unique_symbols(self):
        symbols = [p.symbol for p in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("duplicate symbol")
        return self


def router(user: Callable, db_session: Callable) -> APIRouter:
    app = APIRouter()

    def published_run(db) -> ScreeningRun:
        pointer = db.get(PublishedScreening, "ALL")
        run = db.get(ScreeningRun, pointer.run_id) if pointer else None
        if not run or run.status != "READY":
            raise jobs.JobError("MARKET_NOT_READY", 503)
        return run

    def manifest(db, run) -> DataManifest:
        found = db.get(DataManifest, run.manifest_id) if run.manifest_id else None
        if not found:
            raise jobs.JobError("MARKET_NOT_READY", 503)
        return found

    @app.get("/api/v1/market/latest")
    def market_latest(account=Depends(user), db=Depends(db_session)):
        run = published_run(db)
        source = manifest(db, run)
        universe = db.get(UniverseVersion, source.universe_id)
        members = (universe.payload or {}).get("members", {}) if universe else {}
        items = []
        for row in db.scalars(select(ScreeningResult).where(ScreeningResult.run_id == run.id)):
            summary = (row.payload or {}).get("price_summary") or {}
            meta = members.get(row.symbol, {})
            items.append({"symbol": row.symbol, "name": None,
                "exchange": meta.get("exchange") or row.payload.get("exchange"),
                "sector": meta.get("sector"),
                "price_vnd": summary.get("close"),
                "change_1d_ratio": summary.get("pct_change_1d"),
                "change_5d_ratio": summary.get("pct_change_5d"),
                "change_20d_ratio": summary.get("pct_change_20d"),
                "volume_shares": summary.get("volume")})
        items.sort(key=lambda item: item["symbol"])
        return {"as_of": run.target_session.isoformat(), "run_id": run.id,
            "published_at": run.published_at, "items": items}

    @app.get("/api/v1/market/candles")
    def market_candles(symbol: str = Query(pattern=r"^[A-Z0-9]{2,16}$"),
                       limit: int | None = Query(default=None, ge=1, le=5000),
                       account=Depends(user), db=Depends(db_session)):
        run = published_run(db)
        source = manifest(db, run)
        history_id = (source.histories or {}).get(symbol)
        history = db.get(HistoryRevision, history_id) if history_id else None
        if not history:
            raise jobs.JobError("NOT_FOUND", 404)
        bars = (history.payload or {}).get("bars") or []
        if limit:
            bars = bars[-limit:]
        return {"symbol": symbol, "as_of": run.target_session.isoformat(),
            "bars": [{"session": bar["session"], "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"], "volume": bar["volume"]}
                for bar in bars]}

    def portfolio_view(db, account_id: str) -> dict:
        portfolio = db.scalar(select(Portfolio).where(Portfolio.account_id == account_id))
        if not portfolio:
            return {"cash_vnd": 0, "positions": [], "updated_at": None}
        positions = db.scalars(select(PortfolioPosition)
            .where(PortfolioPosition.portfolio_id == portfolio.id)
            .order_by(PortfolioPosition.symbol))
        return {"cash_vnd": portfolio.cash_vnd, "updated_at": portfolio.updated_at,
            "positions": [{"symbol": p.symbol, "quantity": p.quantity, "cost_vnd": p.cost_vnd}
                for p in positions]}

    @app.get("/api/v1/portfolio")
    def get_portfolio(account=Depends(user), db=Depends(db_session)):
        return portfolio_view(db, account.id)

    @app.put("/api/v1/portfolio")
    def put_portfolio(body: PortfolioInput, account=Depends(user), db=Depends(db_session)):
        portfolio = db.scalar(select(Portfolio).where(Portfolio.account_id == account.id)
            .with_for_update())
        if not portfolio:
            portfolio = Portfolio(account_id=account.id)
            db.add(portfolio)
            db.flush()
        portfolio.cash_vnd = body.cash_vnd
        portfolio.updated_at = int(time.time())
        db.execute(delete(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio.id))
        db.add_all([PortfolioPosition(portfolio_id=portfolio.id, symbol=p.symbol,
            quantity=p.quantity, cost_vnd=p.cost_vnd) for p in body.positions])
        db.commit()
        return portfolio_view(db, account.id)

    return app
