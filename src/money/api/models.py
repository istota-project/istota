"""Pydantic request models for API routes."""

from __future__ import annotations

from pydantic import BaseModel


class QueryRequest(BaseModel):
    bql: str
    ledger: str | None = None


class TransactionRequest(BaseModel):
    date: str
    payee: str
    narration: str
    debit: str
    credit: str
    amount: float
    currency: str = "USD"
    ledger: str | None = None


class WorkEntryRequest(BaseModel):
    date: str
    client: str
    service: str
    qty: float | None = None
    amount: float | None = None
    discount: float = 0
    description: str = ""
    entity: str = ""


class WorkEntryUpdateRequest(BaseModel):
    date: str | None = None
    client: str | None = None
    service: str | None = None
    qty: float | None = None
    amount: float | None = None
    discount: float | None = None
    description: str | None = None
    entity: str | None = None
    invoice: str | None = None


class SyncMonarchRequest(BaseModel):
    dry_run: bool = False
    ledger: str | None = None


class ImportCsvRequest(BaseModel):
    file: str
    account: str
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    ledger: str | None = None


class InvoiceGenerateRequest(BaseModel):
    period: str | None = None
    client: str | None = None
    entity: str | None = None
    dry_run: bool = False


class InvoicePaidRequest(BaseModel):
    invoice_number: str
    date: str
    bank: str | None = None
    no_post: bool = False
    ledger: str | None = None


class InvoiceVoidRequest(BaseModel):
    invoice_number: str
    force: bool = False
    delete_pdf: bool = False


class InvoiceCreateRequest(BaseModel):
    client_key: str
    service: str | None = None
    qty: float | None = None
    description: str | None = None
    items: list[str] | None = None  # "description amount" strings
    entity: str | None = None
