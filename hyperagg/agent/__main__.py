"""Run the Edge Agent: python -m hyperagg.agent [--config /etc/hyperagg/agent.yaml]"""
import argparse
import asyncio
import logging
import signal

from hyperagg.agent.edge_agent import EdgeAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

def main():
    parser = argparse.ArgumentParser(description="HyperAgg Edge Agent")
    parser.add_argument("--config", default="/etc/hyperagg/agent.yaml")
    args = parser.parse_args()

    agent = EdgeAgent(config_path=args.config)

    async def run():
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(agent.shutdown()))
        await agent.run()

    asyncio.run(run())

if __name__ == "__main__":
    main()
