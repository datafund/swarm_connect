# run.py
import uvicorn
import os

if __name__ == "__main__":
    # Default host and port, can be overridden by environment variables
    host = os.getenv("HOST", "127.0.0.1") # Use 127.0.0.1 for local access only
    # host = os.getenv("HOST", "0.0.0.0") # Use 0.0.0.0 to be accessible on the network
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "true").lower() == "true" # Enable reload for development

    # Check for SSL configuration (for local HTTPS testing if needed)
    # You'd need to generate self-signed certs (e.g., using mkcert or openssl)
    # Example: mkcert localhost 127.0.0.1 ::1
    keyfile = os.getenv("SSL_KEYFILE", None) # e.g., "./localhost+2-key.pem"
    certfile = os.getenv("SSL_CERTFILE", None) # e.g., "./localhost+2.pem"

    ssl_options = {}
    if keyfile and certfile and os.path.exists(keyfile) and os.path.exists(certfile):
        print(f"--- Starting HTTPS server on {host}:{port} ---")
        ssl_options = {"ssl_keyfile": keyfile, "ssl_certfile": certfile}
    else:
        print(f"--- Starting HTTP server on {host}:{port} ---")
        if keyfile or certfile:
            print("--- Warning: SSL key/cert file specified but not found. Starting with HTTP. ---")


    uvicorn.run(
        "app.main:app",     # Path to the FastAPI app instance
        host=host,
        port=port,
        reload=reload,      # Automatically reload server on code changes
        log_level="info",   # Set logging level
        server_header=False, # Don't expose server version in headers
        **ssl_options       # Pass SSL options if available
    )
