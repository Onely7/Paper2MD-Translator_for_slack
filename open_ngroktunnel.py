import argparse
import time

from pyngrok import ngrok


def main() -> None:
    """
    Opens a tunnel using ngrok and returns the public URL.
    """
    parser = argparse.ArgumentParser(description="Open a tunnel using ngrok.")
    parser.add_argument("--port", type=str, default="55567", help="Chroma persist dir")
    args = parser.parse_args()

    # Open a tunnel on port
    url = ngrok.connect(args.port)
    # Output ngrok url to console
    print(f"Ingress established at {url}")

    # Keep the listener alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Closing listener")


if __name__ == "__main__":
    # Open a tunnel on port
    main()
