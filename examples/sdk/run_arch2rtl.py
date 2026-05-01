from chiploop_sdk import ChipLoopClient


SPEC = """
Create a simple PWM controller.

Requirements:
- clk input
- active-low rst_n input
- 8-bit duty_cycle input
- pwm_out output
- synthesizable RTL
"""


def main():
    client = ChipLoopClient()
    run = client.run_workflow("arch2rtl", spec_text=SPEC)
    print(f"workflow_id={run.workflow_id}")
    print(f"status={run.status}")


if __name__ == "__main__":
    main()
