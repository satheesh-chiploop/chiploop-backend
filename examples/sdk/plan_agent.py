from chiploop_sdk import ChipLoopClient


def main():
    client = ChipLoopClient()
    result = client.plan_agent(
        {
            "natural_language_request": "Create an RTL lint repair agent for the Digital Loop",
            "domain": "digital",
            "loop_type": "digital",
            "inputs": ["rtl", "lint_errors"],
            "outputs": ["repaired_rtl", "repair_report"],
        }
    )
    print(result)


if __name__ == "__main__":
    main()
