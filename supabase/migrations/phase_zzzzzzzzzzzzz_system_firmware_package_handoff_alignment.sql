-- Reassert the System_Firmware source-of-truth graph after earlier template
-- migrations. With no system_rtl_workflow_id, System_Firmware must first build
-- and package System RTL, then run the PWM-style firmware handoff. Runtime
-- normalization still collapses this to the downstream-only firmware graph when
-- an existing system_rtl_workflow_id is supplied.

with template(name, loop_type, agents) as (
  values
    (
      'System_RTL',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent'
      ]::text[]
    ),
    (
      'System_Sim',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'System CoSim Ingest Agent',
        'System Assertions (SVA) Agent',
        'System Testbench Generator Agent',
        'System Functional Coverage Agent',
        'System Simulation Control Agent',
        'System Simulation Execution Agent',
        'System Simulation Coverage Summary Agent'
      ]::text[]
    ),
    (
      'System_DQA',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'Digital RTL Linting Agent',
        'Digital CDC Analysis Agent',
        'Digital Reset Integrity Agent',
        'Digital Synthesis Readiness Agent',
        'Digital Executive Summary Agent'
      ]::text[]
    ),
    (
      'System_Synthesis',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'Digital Foundry Profile Agent',
        'Digital Implementation Setup Agent',
        'Digital Synthesis Agent',
        'Digital Logic Equivalence Check Agent',
        'Digital DFT Scan Stitching Agent',
        'Digital Scan ATPG Coverage Agent',
        'Digital MBIST Collateral Agent'
      ]::text[]
    ),
    (
      'System_PD',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'Digital Foundry Profile Agent',
        'Digital Implementation Setup Agent',
        'Digital Synthesis Agent',
        'Digital Logic Equivalence Check Agent',
        'Digital DFT Scan Stitching Agent',
        'Digital Scan ATPG Coverage Agent',
        'Digital MBIST Collateral Agent',
        'Digital STA PrePlace Agent',
        'Digital Floorplan Agent',
        'Digital Placement Agent',
        'Digital STA PostPlace Agent',
        'Digital CTS Agent',
        'Digital STA PostCTS Agent',
        'Digital Route Agent',
        'Digital Fill Agent',
        'Digital DRC Agent',
        'Digital LVS Agent',
        'Digital Tapeout Agent',
        'Digital Tapeout Logic Equivalence Check Agent',
        'Digital Executive Summary Agent'
      ]::text[]
    ),
    (
      'System_Firmware',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
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
insert into public.workflows (
  id, user_id, name, loop_type, definitions, status, is_prebuilt, created_at, updated_at
)
select gen_random_uuid(), null, definitions.name, definitions.loop_type, definitions.definition, 'saved', true, now(), now()
from definitions
where not exists (
  select 1
  from public.workflows as workflow
  where workflow.name = definitions.name
    and workflow.user_id is null
);

with template(name, loop_type, agents) as (
  values
    (
      'System_RTL',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent'
      ]::text[]
    ),
    (
      'System_Sim',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'System CoSim Ingest Agent',
        'System Assertions (SVA) Agent',
        'System Testbench Generator Agent',
        'System Functional Coverage Agent',
        'System Simulation Control Agent',
        'System Simulation Execution Agent',
        'System Simulation Coverage Summary Agent'
      ]::text[]
    ),
    (
      'System_DQA',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'Digital RTL Linting Agent',
        'Digital CDC Analysis Agent',
        'Digital Reset Integrity Agent',
        'Digital Synthesis Readiness Agent',
        'Digital Executive Summary Agent'
      ]::text[]
    ),
    (
      'System_Synthesis',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'Digital Foundry Profile Agent',
        'Digital Implementation Setup Agent',
        'Digital Synthesis Agent',
        'Digital Logic Equivalence Check Agent',
        'Digital DFT Scan Stitching Agent',
        'Digital Scan ATPG Coverage Agent',
        'Digital MBIST Collateral Agent'
      ]::text[]
    ),
    (
      'System_PD',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
        'Digital Foundry Profile Agent',
        'Digital Implementation Setup Agent',
        'Digital Synthesis Agent',
        'Digital Logic Equivalence Check Agent',
        'Digital DFT Scan Stitching Agent',
        'Digital Scan ATPG Coverage Agent',
        'Digital MBIST Collateral Agent',
        'Digital STA PrePlace Agent',
        'Digital Floorplan Agent',
        'Digital Placement Agent',
        'Digital STA PostPlace Agent',
        'Digital CTS Agent',
        'Digital STA PostCTS Agent',
        'Digital Route Agent',
        'Digital Fill Agent',
        'Digital DRC Agent',
        'Digital LVS Agent',
        'Digital Tapeout Agent',
        'Digital Tapeout Logic Equivalence Check Agent',
        'Digital Executive Summary Agent'
      ]::text[]
    ),
    (
      'System_Firmware',
      'system',
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital RTL Linting Agent',
        'Digital RTL Signature Agent',
        'Analog Spec Builder Agent',
        'Analog Behavioral Model Agent',
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent',
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
