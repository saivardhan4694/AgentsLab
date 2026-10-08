"""Chat with the AgentLab agent in a terminal.

    set AL_TOKEN_AGENT=...        (the token of the my-agent client in config/clients.yaml)
    python -m agentlab.agent "list the files in my sandbox folder"
    python -m agentlab.agent                                  # interactive

The Gateway must run in HTTP mode: python -m agentlab.gateway.server --http
"""

import argparse
import json
import sys
import uuid

import anyio

from agentlab.agent.agent import Agent, AgentConfig

DIM, RESET = "\033[2m", "\033[0m"


async def turn(agent: Agent, message: str, thread_id: str, show_thinking: bool) -> None:
    thinking = False
    async for e in agent.run(message, thread_id):
        kind = e["type"]
        if kind == "thinking" and show_thinking:
            print(f"{DIM}{e['text']}{RESET}", end="", flush=True)
            thinking = True
        elif kind == "token":
            if thinking:
                print()
                thinking = False
            print(e["text"], end="", flush=True)
        elif kind == "tool_call":
            print(f"\n{DIM}-> {e['name']} {json.dumps(e['args'])}{RESET}", flush=True)
        elif kind == "tool_result":
            text = e["content"].replace("\n", " ")
            mark = "x" if e["is_error"] else "<-"
            print(f"{DIM}{mark} {text[:200]}{'...' if len(text) > 200 else ''}{RESET}", flush=True)
        elif kind == "error":
            print(f"\nerror: {e['message']}", file=sys.stderr)
        elif kind == "done":
            print(f"\n{DIM}[trace {e['trace']}]{RESET}")


async def main_async(args: argparse.Namespace) -> None:
    config = AgentConfig(model=args.model, num_ctx=args.num_ctx, reasoning=not args.no_think, gateway_url=args.url)
    agent = Agent(config)
    thread_id = uuid.uuid4().hex
    if args.message:
        await turn(agent, " ".join(args.message), thread_id, args.show_thinking)
        return
    print(f"AgentLab agent ({config.model}). Empty line to quit.")
    while True:
        try:
            message = input("\n> ").strip()
        except EOFError:
            return
        if not message:
            return
        await turn(agent, message, thread_id, args.show_thinking)


def main() -> None:
    defaults = AgentConfig()
    parser = argparse.ArgumentParser(prog="python -m agentlab.agent", description="AgentLab agent (LangGraph + Ollama)")
    parser.add_argument("message", nargs="*")
    parser.add_argument("--model", default=defaults.model)
    parser.add_argument("--num-ctx", type=int, default=defaults.num_ctx)
    parser.add_argument("--url", default=defaults.gateway_url, help="Gateway MCP endpoint")
    parser.add_argument("--no-think", action="store_true", help="turn off the model's thinking step")
    parser.add_argument("--show-thinking", action="store_true")
    anyio.run(main_async, parser.parse_args())


if __name__ == "__main__":
    main()
