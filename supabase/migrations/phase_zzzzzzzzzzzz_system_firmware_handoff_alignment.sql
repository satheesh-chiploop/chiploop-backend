-- Align System_Firmware with the working PWM Embedded_Run handoff strategy.
-- System_Firmware must import real System RTL/Digital collateral before firmware
-- register extraction so firmware/register_map.json and firmware/hal/registers.rs
-- are derived from upstream artifacts, not fabricated.

with template(name, loop_type, agents) as (
  values
    (
      'System_Firmware',
      'system',
      array[
        'Embedded Digital RTL Handoff Ingest Agent',
        'Embedded Firmware Register Extract Agent',
        'Embedded Rust Register Layer Generator Agent',
        'Embedded Register Validation Agent',
        'Embedded Rust Driver Scaffold Agent',
        'Embedded Interrupt Mapping Agent',
        'Embedded Firmware Integration Contract Agent',
        'Embedded ELF Build Agent',
        'Embedded Verilator Build Agent',
        'Embedded Cocotb Harness Agent',
        'Embedded Co Sim Runner Agent',
        'System Firmware CoSim Execution Agent',
        'System Firmware Coverage Summary Agent',
        'Embedded Coverage Collector Agent',
        'Embedded Validation Report Agent',
        'Embedded Firmware Executive Summary Agent',
        'System Software Handoff Package Agent'
      ]::text[]
    )
),
definitions as (
  select
    template.name,
    template.loop_type,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agentNode',
            'position', jsonb_build_object(
              'x', 80 + ((ord - 1) % 4) * 372,
              'y', 160 + floor((ord - 1) / 4) * 210
            ),
            'data', jsonb_build_object(
              'desc', 'Auto-composed: ' || agent_name,
              'uiLabel', agent_name,
              'backendLabel', agent_name
            )
          )
          order by ord
        )
        from unnest(template.agents) with ordinality as ordered(agent_name, ord)
      ),
      'edges',
      (
        select coalesce(
          jsonb_agg(
            jsonb_build_object(
              'id', 'e' || (ord - 1),
              'source', 'n' || (ord - 1),
              'target', 'n' || ord
            )
            order by ord
          ),
          '[]'::jsonb
        )
        from generate_series(2, array_length(template.agents, 1)) as edge_ord(ord)
      )
    ) as definition
  from template
)
update public.workflows as workflow
set definitions = definitions.definition,
    loop_type = definitions.loop_type,
    is_prebuilt = true,
    updated_at = now()
from definitions
where workflow.name = definitions.name
  and workflow.user_id is null;
