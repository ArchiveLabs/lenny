import argparse
import sys
import os
from dotenv import load_dotenv

load_dotenv()

from lenny.core.models import Client
from lenny.core.db import session, init as db_init

def main():
    parser = argparse.ArgumentParser(description="Register a new LennyOAuth Client")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--redirect-uris", required=True, help="Comma separated list")
    args = parser.parse_args()
    
    try:
        db_init()
        existing = session.query(Client).filter(Client.client_id == args.client_id).first()
        if existing:
            existing.redirect_uris = args.redirect_uris
            session.commit()
            print(f"Success! Client '{args.client_id}' updated with new redirect URIs.")
            return
            
        client = Client(
            client_id=args.client_id,
            redirect_uris=args.redirect_uris,
            is_confidential=False  # Lenny uses public clients w/ PKCE
        )
        session.add(client)
        session.commit()
        print(f"Success! Client '{args.client_id}' created.")
    except Exception as e:
        session.rollback()
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        session.close()

if __name__ == "__main__":
    main()
