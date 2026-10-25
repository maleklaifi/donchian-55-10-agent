import importlib.util,json,pathlib,pandas as pd
p=pathlib.Path(__file__).parent/'extracted/v3/scripts/06_frozen_comparison.py'
s=importlib.util.spec_from_file_location('comparison',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
out=pathlib.Path(__file__).parent/'risk_comparison';out.mkdir(exist_ok=True)
rows=[]
for capital in (200,1000):
 for risk in (.005,.01):
  m.INITIAL_CAPITAL=capital;m.RISK_PER_TRADE=risk
  summary,eq,ex,pos=m.run_fixed(55,10,2)
  summary.update(initial_capital=capital,risk_percent=risk*100)
  rows.append(summary)
  eq.to_csv(out/f'equity_{capital}_{risk}.csv',index=False)
  ex.to_csv(out/f'executions_{capital}_{risk}.csv',index=False)
  print(json.dumps(summary),flush=True)
pd.DataFrame(rows).to_csv(out/'summary.csv',index=False)
