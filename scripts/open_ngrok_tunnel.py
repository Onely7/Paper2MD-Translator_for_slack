"""Open an ngrok tunnel to a local port and keep it open (uses NGROK_AUTHTOKEN)."""

import argparse
import time

from pyngrok import ngrok


def main() -> None:
    parser = argparse.ArgumentParser(description="Open an ngrok tunnel to the Slack bot.")
    parser.add_argument("--port", default="8888", help="local port of the bot (default: 8888)")
    args = parser.parse_args()

    tunnel = ngrok.connect(args.port)
    print(f"Ingress established at {tunnel}")
    print(f"Slack Request URL: {tunnel.public_url}/slack/events")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Closing the tunnel")


if __name__ == "__main__":
    main()
