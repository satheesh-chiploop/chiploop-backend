class BaseAgent:
    """
    Every ChipLoop agent inherits this.
    Guarantees standard execution + return structure.
    """

    async def run(self, state):
        """
        Implement in subclass.
        Must return:
        {
            "artifact": <filepath or None>,
            "log": <string summary>,
            "code": <generated text or None>
        }
        """
        raise NotImplementedError("Agent must implement run()")
