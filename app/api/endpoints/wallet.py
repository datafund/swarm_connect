# app/api/endpoints/wallet.py
from fastapi import APIRouter, HTTPException
from requests.exceptions import RequestException
import logging

from app.services.swarm_api import get_wallet_address, get_chequebook_address
from app.api.models.wallet import WalletResponse, ChequebookResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/wallet", response_model=WalletResponse)
async def get_wallet() -> WalletResponse:
    """
    Get the wallet address of the Bee node.

    Returns:
        WalletResponse: Object containing the wallet address

    Raises:
        HTTPException: 500 if unable to fetch wallet address from Swarm API
    """
    try:
        wallet_address = get_wallet_address()
        logger.info(f"Wallet endpoint accessed, returning address: {wallet_address}")
        return WalletResponse(walletAddress=wallet_address)

    except RequestException as e:
        logger.error(f"Failed to fetch wallet address: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch wallet address from Swarm API"
        )
    except ValueError as e:
        logger.error(f"Invalid wallet response: {e}")
        raise HTTPException(
            status_code=500,
            detail="Invalid response from Swarm API"
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching wallet address: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred"
        )


@router.get("/chequebook/address", response_model=ChequebookResponse)
async def get_chequebook() -> ChequebookResponse:
    """
    Get the chequebook address of the Bee node.

    Returns:
        ChequebookResponse: Object containing the chequebook address

    Raises:
        HTTPException: 500 if unable to fetch chequebook address from Swarm API
    """
    try:
        chequebook_address = get_chequebook_address()
        logger.info(f"Chequebook endpoint accessed, returning address: {chequebook_address}")
        return ChequebookResponse(chequebookAddress=chequebook_address)

    except RequestException as e:
        logger.error(f"Failed to fetch chequebook address: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch chequebook address from Swarm API"
        )
    except ValueError as e:
        logger.error(f"Invalid chequebook response: {e}")
        raise HTTPException(
            status_code=500,
            detail="Invalid response from Swarm API"
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching chequebook address: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred"
        )