Create a simple PWM controller.

Requirements:

- Input clock: clk
- Active-low reset: rst_n
- 8-bit duty cycle input: duty_cycle
- 8-bit counter
- Output: pwm_out
- pwm_out is high while counter is less than duty_cycle
- Include clear, synthesizable RTL
