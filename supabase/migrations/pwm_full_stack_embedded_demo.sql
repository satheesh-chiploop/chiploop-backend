-- Publish the corrected Embedded_Run graph for the guided PWM full-stack demo.
-- App executions remain user-owned; only the shared template row is inserted or updated.

with embedded_run_definition(definitions) as (
  values (
    '{
      "nodes": [
        {"id":"n1","type":"agentNode","position":{"x":80,"y":160},"data":{"uiLabel":"Digital RTL Handoff Ingest","backendLabel":"Embedded Digital RTL Handoff Ingest Agent"}},
        {"id":"n2","type":"agentNode","position":{"x":452,"y":160},"data":{"uiLabel":"Firmware Register Extract","backendLabel":"Embedded Firmware Register Extract Agent"}},
        {"id":"n3","type":"agentNode","position":{"x":824,"y":160},"data":{"uiLabel":"Rust Register Layer","backendLabel":"Embedded Rust Register Layer Generator Agent"}},
        {"id":"n4","type":"agentNode","position":{"x":1196,"y":160},"data":{"uiLabel":"Register Validation","backendLabel":"Embedded Register Validation Agent"}},
        {"id":"n5","type":"agentNode","position":{"x":80,"y":370},"data":{"uiLabel":"Rust Driver Scaffold","backendLabel":"Embedded Rust Driver Scaffold Agent"}},
        {"id":"n6","type":"agentNode","position":{"x":452,"y":370},"data":{"uiLabel":"Interrupt Mapping","backendLabel":"Embedded Interrupt Mapping Agent"}},
        {"id":"n7","type":"agentNode","position":{"x":824,"y":370},"data":{"uiLabel":"Firmware Contract","backendLabel":"Embedded Firmware Integration Contract Agent"}},
        {"id":"n8","type":"agentNode","position":{"x":1196,"y":370},"data":{"uiLabel":"ELF Build","backendLabel":"Embedded ELF Build Agent"}},
        {"id":"n9","type":"agentNode","position":{"x":80,"y":580},"data":{"uiLabel":"Verilator Build","backendLabel":"Embedded Verilator Build Agent"}},
        {"id":"n10","type":"agentNode","position":{"x":452,"y":580},"data":{"uiLabel":"Cocotb Harness","backendLabel":"Embedded Cocotb Harness Agent"}},
        {"id":"n11","type":"agentNode","position":{"x":824,"y":580},"data":{"uiLabel":"Co-Sim Runner","backendLabel":"Embedded Co Sim Runner Agent"}},
        {"id":"n12","type":"agentNode","position":{"x":1196,"y":580},"data":{"uiLabel":"Firmware Co-Sim Execution","backendLabel":"System Firmware CoSim Execution Agent"}},
        {"id":"n13","type":"agentNode","position":{"x":80,"y":790},"data":{"uiLabel":"Firmware Coverage","backendLabel":"System Firmware Coverage Summary Agent"}},
        {"id":"n14","type":"agentNode","position":{"x":452,"y":790},"data":{"uiLabel":"Coverage Collector","backendLabel":"Embedded Coverage Collector Agent"}},
        {"id":"n15","type":"agentNode","position":{"x":824,"y":790},"data":{"uiLabel":"Validation Report","backendLabel":"Embedded Validation Report Agent"}},
        {"id":"n16","type":"agentNode","position":{"x":1196,"y":790},"data":{"uiLabel":"Firmware Summary","backendLabel":"Embedded Firmware Executive Summary Agent"}},
        {"id":"n17","type":"agentNode","position":{"x":80,"y":1000},"data":{"uiLabel":"Software Handoff","backendLabel":"System Software Handoff Package Agent"}}
      ],
      "edges": [
        {"id":"e1","source":"n1","target":"n2"},{"id":"e2","source":"n2","target":"n3"},
        {"id":"e3","source":"n3","target":"n4"},{"id":"e4","source":"n4","target":"n5"},
        {"id":"e5","source":"n5","target":"n6"},{"id":"e6","source":"n6","target":"n7"},
        {"id":"e7","source":"n7","target":"n8"},{"id":"e8","source":"n8","target":"n9"},
        {"id":"e9","source":"n9","target":"n10"},{"id":"e10","source":"n10","target":"n11"},
        {"id":"e11","source":"n11","target":"n12"},{"id":"e12","source":"n12","target":"n13"},
        {"id":"e13","source":"n13","target":"n14"},{"id":"e14","source":"n14","target":"n15"},
        {"id":"e15","source":"n15","target":"n16"},{"id":"e16","source":"n16","target":"n17"}
      ]
    }'::jsonb
  )
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(), null, 'Embedded_Run', 'embedded', definitions, 'saved', true, now(), now()
from embedded_run_definition
where not exists (
  select 1 from public.workflows
  where name = 'Embedded_Run' and user_id is null
);

with embedded_run_definition(definitions) as (
  values (
    '{
      "nodes": [
        {"id":"n1","type":"agentNode","position":{"x":80,"y":160},"data":{"uiLabel":"Digital RTL Handoff Ingest","backendLabel":"Embedded Digital RTL Handoff Ingest Agent"}},
        {"id":"n2","type":"agentNode","position":{"x":452,"y":160},"data":{"uiLabel":"Firmware Register Extract","backendLabel":"Embedded Firmware Register Extract Agent"}},
        {"id":"n3","type":"agentNode","position":{"x":824,"y":160},"data":{"uiLabel":"Rust Register Layer","backendLabel":"Embedded Rust Register Layer Generator Agent"}},
        {"id":"n4","type":"agentNode","position":{"x":1196,"y":160},"data":{"uiLabel":"Register Validation","backendLabel":"Embedded Register Validation Agent"}},
        {"id":"n5","type":"agentNode","position":{"x":80,"y":370},"data":{"uiLabel":"Rust Driver Scaffold","backendLabel":"Embedded Rust Driver Scaffold Agent"}},
        {"id":"n6","type":"agentNode","position":{"x":452,"y":370},"data":{"uiLabel":"Interrupt Mapping","backendLabel":"Embedded Interrupt Mapping Agent"}},
        {"id":"n7","type":"agentNode","position":{"x":824,"y":370},"data":{"uiLabel":"Firmware Contract","backendLabel":"Embedded Firmware Integration Contract Agent"}},
        {"id":"n8","type":"agentNode","position":{"x":1196,"y":370},"data":{"uiLabel":"ELF Build","backendLabel":"Embedded ELF Build Agent"}},
        {"id":"n9","type":"agentNode","position":{"x":80,"y":580},"data":{"uiLabel":"Verilator Build","backendLabel":"Embedded Verilator Build Agent"}},
        {"id":"n10","type":"agentNode","position":{"x":452,"y":580},"data":{"uiLabel":"Cocotb Harness","backendLabel":"Embedded Cocotb Harness Agent"}},
        {"id":"n11","type":"agentNode","position":{"x":824,"y":580},"data":{"uiLabel":"Co-Sim Runner","backendLabel":"Embedded Co Sim Runner Agent"}},
        {"id":"n12","type":"agentNode","position":{"x":1196,"y":580},"data":{"uiLabel":"Firmware Co-Sim Execution","backendLabel":"System Firmware CoSim Execution Agent"}},
        {"id":"n13","type":"agentNode","position":{"x":80,"y":790},"data":{"uiLabel":"Firmware Coverage","backendLabel":"System Firmware Coverage Summary Agent"}},
        {"id":"n14","type":"agentNode","position":{"x":452,"y":790},"data":{"uiLabel":"Coverage Collector","backendLabel":"Embedded Coverage Collector Agent"}},
        {"id":"n15","type":"agentNode","position":{"x":824,"y":790},"data":{"uiLabel":"Validation Report","backendLabel":"Embedded Validation Report Agent"}},
        {"id":"n16","type":"agentNode","position":{"x":1196,"y":790},"data":{"uiLabel":"Firmware Summary","backendLabel":"Embedded Firmware Executive Summary Agent"}},
        {"id":"n17","type":"agentNode","position":{"x":80,"y":1000},"data":{"uiLabel":"Software Handoff","backendLabel":"System Software Handoff Package Agent"}}
      ],
      "edges": [
        {"id":"e1","source":"n1","target":"n2"},{"id":"e2","source":"n2","target":"n3"},
        {"id":"e3","source":"n3","target":"n4"},{"id":"e4","source":"n4","target":"n5"},
        {"id":"e5","source":"n5","target":"n6"},{"id":"e6","source":"n6","target":"n7"},
        {"id":"e7","source":"n7","target":"n8"},{"id":"e8","source":"n8","target":"n9"},
        {"id":"e9","source":"n9","target":"n10"},{"id":"e10","source":"n10","target":"n11"},
        {"id":"e11","source":"n11","target":"n12"},{"id":"e12","source":"n12","target":"n13"},
        {"id":"e13","source":"n13","target":"n14"},{"id":"e14","source":"n14","target":"n15"},
        {"id":"e15","source":"n15","target":"n16"},{"id":"e16","source":"n16","target":"n17"}
      ]
    }'::jsonb
  )
)
update public.workflows as workflow
set definitions = definition.definitions,
    is_prebuilt = true,
    updated_at = now()
from embedded_run_definition as definition
where workflow.name = 'Embedded_Run'
  and workflow.user_id is null;

-- Arch2RTL must publish the register map consumed by the firmware demo.
update public.workflows as workflow
set definitions = jsonb_build_object(
      'nodes', jsonb_build_array(
        jsonb_build_object('id','n1','type','agentNode','position',jsonb_build_object('x',80,'y',160),'data',jsonb_build_object('uiLabel','Digital Spec','backendLabel','Digital Spec Agent')),
        jsonb_build_object('id','n2','type','agentNode','position',jsonb_build_object('x',320,'y',160),'data',jsonb_build_object('uiLabel','Digital Architecture','backendLabel','Digital Architecture Agent')),
        jsonb_build_object('id','n3','type','agentNode','position',jsonb_build_object('x',560,'y',160),'data',jsonb_build_object('uiLabel','Digital Microarchitecture','backendLabel','Digital Microarchitecture Agent')),
        jsonb_build_object('id','n4','type','agentNode','position',jsonb_build_object('x',800,'y',160),'data',jsonb_build_object('uiLabel','Digital Register Map','backendLabel','Digital Register Map Agent')),
        jsonb_build_object('id','n5','type','agentNode','position',jsonb_build_object('x',1040,'y',160),'data',jsonb_build_object('uiLabel','Digital RTL','backendLabel','Digital RTL Agent')),
        jsonb_build_object('id','n6','type','agentNode','position',jsonb_build_object('x',1280,'y',160),'data',jsonb_build_object('uiLabel','Digital Power Intent','backendLabel','Digital Power Intent (UPF-lite) Agent')),
        jsonb_build_object('id','n7','type','agentNode','position',jsonb_build_object('x',1520,'y',160),'data',jsonb_build_object('uiLabel','Digital Package','backendLabel','Digital IP Packaging & Handoff Agent'))
      ),
      'edges', jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4'),
        jsonb_build_object('id','e4','source','n4','target','n5'),
        jsonb_build_object('id','e5','source','n5','target','n6'),
        jsonb_build_object('id','e6','source','n6','target','n7')
      )
    ),
    is_prebuilt = true,
    updated_at = now()
where workflow.name = 'Digital_Arch2RTL'
  and workflow.user_id is null;

-- Verification must execute simulation and publish structured coverage evidence.
update public.workflows as workflow
set definitions = jsonb_build_object(
      'nodes', jsonb_build_array(
        jsonb_build_object('id','n1','type','agentNode','position',jsonb_build_object('x',80,'y',160),'data',jsonb_build_object('uiLabel','RTL Handoff Ingest','backendLabel','Digital Verification Handoff Ingest Agent')),
        jsonb_build_object('id','n2','type','agentNode','position',jsonb_build_object('x',320,'y',160),'data',jsonb_build_object('uiLabel','Functional Coverage','backendLabel','Digital Functional Coverage Agent')),
        jsonb_build_object('id','n3','type','agentNode','position',jsonb_build_object('x',560,'y',160),'data',jsonb_build_object('uiLabel','Testbench Generator','backendLabel','Digital Testbench Generator Agent')),
        jsonb_build_object('id','n4','type','agentNode','position',jsonb_build_object('x',800,'y',160),'data',jsonb_build_object('uiLabel','Assertions','backendLabel','Digital Assertions (SVA) Agent')),
        jsonb_build_object('id','n5','type','agentNode','position',jsonb_build_object('x',1040,'y',160),'data',jsonb_build_object('uiLabel','Simulation Control','backendLabel','Digital Simulation Control Agent')),
        jsonb_build_object('id','n6','type','agentNode','position',jsonb_build_object('x',1280,'y',160),'data',jsonb_build_object('uiLabel','Simulation Execution','backendLabel','Digital Simulation Execution Agent')),
        jsonb_build_object('id','n7','type','agentNode','position',jsonb_build_object('x',1520,'y',160),'data',jsonb_build_object('uiLabel','Summary Coverage','backendLabel','Digital Simulation Summary Coverage Agent'))
      ),
      'edges', jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4'),
        jsonb_build_object('id','e4','source','n4','target','n5'),
        jsonb_build_object('id','e5','source','n5','target','n6'),
        jsonb_build_object('id','e6','source','n6','target','n7')
      )
    ),
    is_prebuilt = true,
    updated_at = now()
where workflow.name = 'Digital_Verify'
  and workflow.user_id is null;

-- Software and full validation publish the downstream demo dashboard evidence.
update public.workflows as workflow
set definitions = jsonb_build_object(
      'nodes', jsonb_build_array(
        jsonb_build_object('id','n1','type','agentNode','position',jsonb_build_object('x',80,'y',160),'data',jsonb_build_object('uiLabel','Software Handoff Ingest','backendLabel','System Software Handoff Ingest Agent')),
        jsonb_build_object('id','n2','type','agentNode','position',jsonb_build_object('x',340,'y',160),'data',jsonb_build_object('uiLabel','Capability Model','backendLabel','System Software Capability Model Agent')),
        jsonb_build_object('id','n3','type','agentNode','position',jsonb_build_object('x',600,'y',160),'data',jsonb_build_object('uiLabel','API Contract','backendLabel','System Software API Contract Agent')),
        jsonb_build_object('id','n4','type','agentNode','position',jsonb_build_object('x',860,'y',160),'data',jsonb_build_object('uiLabel','SDK Scaffold','backendLabel','System Software SDK Scaffold Agent')),
        jsonb_build_object('id','n5','type','agentNode','position',jsonb_build_object('x',1120,'y',160),'data',jsonb_build_object('uiLabel','Application Scaffold','backendLabel','System Software Application Scaffold Agent')),
        jsonb_build_object('id','n6','type','agentNode','position',jsonb_build_object('x',1380,'y',160),'data',jsonb_build_object('uiLabel','Packaging','backendLabel','System Software Packaging Agent'))
      ),
      'edges', jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4'),
        jsonb_build_object('id','e4','source','n4','target','n5'),
        jsonb_build_object('id','e5','source','n5','target','n6')
      )
    ),
    is_prebuilt = true,
    updated_at = now()
where workflow.name = 'System_Software'
  and workflow.user_id is null;

update public.workflows as workflow
set definitions = jsonb_build_object(
      'nodes', jsonb_build_array(
        jsonb_build_object('id','n1','type','agentNode','position',jsonb_build_object('x',80,'y',160),'data',jsonb_build_object('uiLabel','Validation Ingest','backendLabel','System Software Validation Ingest Agent')),
        jsonb_build_object('id','n2','type','agentNode','position',jsonb_build_object('x',340,'y',160),'data',jsonb_build_object('uiLabel','CoSim Ingest','backendLabel','System CoSim Ingest Agent')),
        jsonb_build_object('id','n3','type','agentNode','position',jsonb_build_object('x',600,'y',160),'data',jsonb_build_object('uiLabel','CoSim Contract','backendLabel','System CoSim Contract Agent')),
        jsonb_build_object('id','n4','type','agentNode','position',jsonb_build_object('x',860,'y',160),'data',jsonb_build_object('uiLabel','Scenario Generator','backendLabel','System CoSim Scenario Generator Agent')),
        jsonb_build_object('id','n5','type','agentNode','position',jsonb_build_object('x',80,'y',380),'data',jsonb_build_object('uiLabel','CoSim Harness','backendLabel','System Software CoSim Harness Agent')),
        jsonb_build_object('id','n6','type','agentNode','position',jsonb_build_object('x',340,'y',380),'data',jsonb_build_object('uiLabel','CoSim Execution','backendLabel','System Software CoSim Execution Agent')),
        jsonb_build_object('id','n7','type','agentNode','position',jsonb_build_object('x',600,'y',380),'data',jsonb_build_object('uiLabel','Trace Validation','backendLabel','System Software CoSim Trace Validation Agent')),
        jsonb_build_object('id','n8','type','agentNode','position',jsonb_build_object('x',860,'y',380),'data',jsonb_build_object('uiLabel','Validation Summary','backendLabel','System Software Validation Summary (L2)'))
      ),
      'edges', jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4'),
        jsonb_build_object('id','e4','source','n4','target','n5'),
        jsonb_build_object('id','e5','source','n5','target','n6'),
        jsonb_build_object('id','e6','source','n6','target','n7'),
        jsonb_build_object('id','e7','source','n7','target','n8')
      )
    ),
    is_prebuilt = true,
    updated_at = now()
where workflow.name = 'System_Software_Validation_L2'
  and workflow.user_id is null;
