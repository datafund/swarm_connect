# app/api/models/wallet.py
from pydantic import BaseModel


class WalletResponse(BaseModel):
    """
    Response model for wallet address endpoint.
    """
    walletAddress: str


class ChequebookResponse(BaseModel):
    """
    Response model for chequebook address endpoint.
    """
    chequebookAddress: str