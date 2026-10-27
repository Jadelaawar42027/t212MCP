"""Pydantic models for the Trading 212 responses we read.

Every field is optional and extra fields are kept, so upstream additions or
missing values never break a tool. Shapes follow the OpenAPI spec.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _M(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Instrument(_M):
    ticker: str | None = None
    name: str | None = None
    isin: str | None = None
    currency: str | None = None


class Cash(_M):
    availableToTrade: float | None = None
    inPies: float | None = None
    reservedForOrders: float | None = None


class Investments(_M):
    currentValue: float | None = None
    realizedProfitLoss: float | None = None
    totalCost: float | None = None
    unrealizedProfitLoss: float | None = None


class AccountSummary(_M):
    id: int | None = None
    currency: str | None = None
    totalValue: float | None = None
    cash: Cash = Cash()
    investments: Investments = Investments()


class PositionWalletImpact(_M):
    currency: str | None = None
    currentValue: float | None = None
    fxImpact: float | None = None
    totalCost: float | None = None
    unrealizedProfitLoss: float | None = None


class Position(_M):
    instrument: Instrument = Instrument()
    quantity: float | None = None
    quantityAvailableForTrading: float | None = None
    quantityInPies: float | None = None
    averagePricePaid: float | None = None
    currentPrice: float | None = None
    createdAt: datetime | None = None
    walletImpact: PositionWalletImpact = PositionWalletImpact()

    @property
    def ticker(self) -> str:
        return self.instrument.ticker or "?"


class Tax(_M):
    name: str | None = None
    quantity: float | None = None
    currency: str | None = None
    chargedAt: datetime | None = None


class FillWalletImpact(_M):
    currency: str | None = None
    fxRate: float | None = None
    netValue: float | None = None
    realisedProfitLoss: float | None = None
    taxes: list[Tax] = []


class Fill(_M):
    id: int | None = None
    filledAt: datetime | None = None
    price: float | None = None
    quantity: float | None = None
    type: str | None = None
    tradingMethod: str | None = None
    walletImpact: FillWalletImpact | None = None


class Order(_M):
    id: int | None = None
    createdAt: datetime | None = None
    ticker: str | None = None
    instrument: Instrument | None = None
    side: str | None = None
    type: str | None = None
    status: str | None = None
    strategy: str | None = None
    currency: str | None = None
    quantity: float | None = None
    filledQuantity: float | None = None
    value: float | None = None
    filledValue: float | None = None
    limitPrice: float | None = None
    stopPrice: float | None = None
    initiatedFrom: str | None = None


class HistoricalOrder(_M):
    order: Order = Order()
    fill: Fill | None = None

    @property
    def when(self) -> datetime | None:
        return (self.fill.filledAt if self.fill else None) or self.order.createdAt


class Dividend(_M):
    ticker: str | None = None
    instrument: Instrument | None = None
    amount: float | None = None
    amountInEuro: float | None = None
    currency: str | None = None
    tickerCurrency: str | None = None
    grossAmountPerShare: float | None = None
    quantity: float | None = None
    paidOn: datetime | None = None
    type: str | None = None
    reference: str | None = None


class Transaction(_M):
    type: str | None = None
    amount: float | None = None
    currency: str | None = None
    dateTime: datetime | None = None
    reference: str | None = None


class InvestmentResult(_M):
    priceAvgInvestedValue: float | None = None
    priceAvgResult: float | None = None
    priceAvgResultCoef: float | None = None
    priceAvgValue: float | None = None


class DividendDetails(_M):
    gained: float | None = None
    inCash: float | None = None
    reinvested: float | None = None


class Pie(_M):
    id: int | None = None
    cash: float | None = None
    progress: float | None = None
    status: str | None = None
    result: InvestmentResult = InvestmentResult()
    dividendDetails: DividendDetails = DividendDetails()


class PieInstrument(_M):
    ticker: str | None = None
    currentShare: float | None = None
    expectedShare: float | None = None
    ownedQuantity: float | None = None
    result: InvestmentResult = InvestmentResult()
    issues: list[dict] = []


class PieSettings(_M):
    id: int | None = None
    name: str | None = None
    goal: float | None = None
    icon: str | None = None
    dividendCashAction: str | None = None
    creationDate: datetime | None = None
    endDate: datetime | None = None
    initialInvestment: float | None = None
    publicUrl: str | None = None


class PieDetail(_M):
    instruments: list[PieInstrument] = []
    settings: PieSettings = PieSettings()


class TradableInstrument(_M):
    ticker: str | None = None
    name: str | None = None
    shortName: str | None = None
    isin: str | None = None
    currencyCode: str | None = None
    type: str | None = None
    workingScheduleId: int | None = None
    addedOn: datetime | None = None
    extendedHours: bool | None = None
    maxOpenQuantity: float | None = None


class WorkingSchedule(_M):
    id: int | None = None


class Exchange(_M):
    id: int | None = None
    name: str | None = None
    workingSchedules: list[WorkingSchedule] = []
