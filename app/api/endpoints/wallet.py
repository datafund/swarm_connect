# app/api/endpoints/wallet.py
from fastapi import APIRouter, HTTPException
import httpx
import logging

from app.services.swarm_api import get_wallet_info, get_chequebook_info
from app.api.models.wallet import WalletResponse, ChequebookResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/wallet", response_model=WalletResponse)
async def get_wallet() -> WalletResponse:
    """
    Get the wallet address and BZZ balance of the Bee node.

    Returns:
        WalletResponse: Object containing the wallet address and BZZ balance

    Raises:
        HTTPException: 500 if unable to fetch wallet information from Swarm API
    """
    try:
        wallet_info = await get_wallet_info()
        logger.info(f"Wallet endpoint accessed, returning address: {wallet_info.get('walletAddress')}, balance: {wallet_info.get('bzzBalance')}")
        return WalletResponse(
            walletAddress=wallet_info["walletAddress"],
            bzzBalance=wallet_info.get("bzzBalance")
        )

    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch wallet information: {e}")
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch wallet information from Swarm API"
        )
    except ValueError as e:
        logger.error(f"Invalid wallet response: {e}")
        raise HTTPException(
            status_code=500,
            detail="Invalid response from Swarm API"
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching wallet information: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred"
        )


@router.get("/chequebook", response_model=ChequebookResponse)
async def get_chequebook() -> ChequebookResponse:
    """
    Get the chequebook address and balance information of the Bee node.

    Returns:
        ChequebookResponse: Object containing the chequebook address and balance information

    Raises:
        HTTPException: 500 if unable to fetch chequebook information from Swarm API
    """
    try:
        chequebook_info = await get_chequebook_info()
        logger.info(f"Chequebook endpoint accessed, returning address: {chequebook_info.get('chequebookAddress')}, available: {chequebook_info.get('availableBalance')}")
        return ChequebookResponse(
            chequebookAddress=chequebook_info["chequebookAddress"],
            availableBalance=chequebook_info.get("availableBalance"),
            totalBalance=chequebook_info.get("totalBalance")
        )

    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch chequebook information: {e}")
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch chequebook information from Swarm API"
        )
    except ValueError as e:
        logger.error(f"Invalid chequebook response: {e}")
        raise HTTPException(
            status_code=500,
            detail="Invalid response from Swarm API"
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching chequebook information: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred"
        )