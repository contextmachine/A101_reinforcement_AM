import copy
from collections import deque
from rebar_service.config import Settings
from rebar_service.pipeline import PipelineWorkflow, PipelineJob
from rebar_service.scene_geometry import build_stable_scene_components
from rebar_service.overlays import resolve_overlay

class MemoryStore:
    def __init__(self, axis='y'):
        self.settings=Settings(solver_backend='scipy',fit_milp_backend='scipy',grid_size=300,fill_notches=0,short_edge=0,simplify_step=0,use_mosaic=False)
        self.rows=[{'points':[[0,0],[1200,0],[1200,1800],[0,1800]],'load':12.0},
                   {'points':[[1800,0],[3000,0],[3000,1800],[1800,1800]],'load':19.0}]
        self.meta={'scene_id':'scene', 'initial_variant':'raw','analysis_variant':'raw','analysis_overlay_id':0,
                   'parameters':{'axis':axis,'anchor_factor':40,'back_grid':[16,300],'stock':[[16,300],[20,150],[20,100]],
                                 'max_layers':2,'min_width_mm':300,'solver':{'backend':'scipy','threads':1}},
                   'requested_n':[1,2,3,4], 'whole':True,'component_selection':[-2], 'scan_mode':'requested', 'component_result_top_k':3}
        self.components={};self.problems={};self.frontiers={};self.solved={};self.fields={};self.candidates={};self.solutions={}
        self.jobs=deque();self.dedupe=set();self.enqueued=[];self.events=[];self.analysis={'preparation_state':'stored'}
    def get_meta(self,t):return dict(self.meta)
    def generation(self,t):return 0
    def pending_jobs(self,t):return len(self.jobs)
    def patch_meta(self,t,**v):self.meta.update(v);return dict(self.meta)
    def get_object(self,*a):return {'kind':'polygons','polygons':self.rows}
    def load_variant_polygons(self,*a,**kw):return copy.deepcopy(self.rows)
    def scene_components(self,*a):return build_stable_scene_components(self.rows)
    def resolved_source_polygons(self,*a,**kw):return resolve_overlay(self.rows,[],0)
    def ensure_analysis(self,*a,**kw):return self.analysis
    def analysis_state(self,*a,**kw):return dict(self.analysis)
    def mark_analysis_preparing(self,*a,**kw):self.analysis['preparation_state']='preparing';return True
    def mark_analysis_prepared(self,*a,**kw):self.analysis['preparation_state']='prepared'
    def requested_ns(self,*a,**kw):return self.meta['requested_n']
    def add_requested_ns(self,t,v,**kw):self.meta['requested_n']=list(dict.fromkeys(self.meta['requested_n']+list(v)))
    def is_n_cancelled(self,*a,**kw):return False
    def enqueue_pipeline_job(self,j):
        if j['dedupe_key'] in self.dedupe:return False
        self.dedupe.add(j['dedupe_key']);self.jobs.append(j);self.enqueued.append(j);return True
    def publish_event(self,t,e,p,**kw):self.events.append((e,p));return '1'
    def save_field(self,t,f,**kw):self.fields[t]=copy.deepcopy(f)
    def load_field(self,t,**kw):return self.fields.get(t)
    def save_component(self,t,c,v,**kw):self.components[str(c)]=copy.deepcopy(v)
    def load_component(self,t,c,**kw):return copy.deepcopy(self.components.get(str(c)))
    def component_ids(self,t,**kw):return sorted(self.components,key=lambda x:(x=='whole',x))
    def save_problem(self,t,c,v,**kw):self.problems[str(c)]=v
    def load_problem(self,t,c,**kw):return self.problems.get(str(c))
    def save_solver_result(self,t,c,n,v,**kw):self.solved[str(c),n]=v
    def load_solver_result(self,t,c,n,**kw):return self.solved.get((str(c),n))
    def delete_solver_result(self,t,c,n,**kw):self.solved.pop((str(c),n),None)
    def save_frontier_result(self,t,c,n,v,**kw):self.frontiers.setdefault(str(c),{})[n]=copy.deepcopy(v)
    def load_frontier(self,t,c,**kw):return copy.deepcopy(self.frontiers.get(str(c),{}))
    def all_frontiers(self,t,**kw):return {int(c):copy.deepcopy(v) for c,v in self.frontiers.items() if c!='whole'}
    def frontier_version(self,*a,**kw):return sum(len(f) for f in self.frontiers.values())
    def save_candidate(self,t,c,v):self.candidates[c]=copy.deepcopy(v)
    def load_candidate(self,t,c,**kw):return copy.deepcopy(self.candidates.get(c))
    def save_solution(self,t,v):self.solutions[v['solution_id']]=copy.deepcopy(v)
    def load_solution(self,t,s,**kw):return self.solutions.get(s)
    def best_solution(self,t,n,**kw):
        vs=[s for s in self.solutions.values() if s['total_N']==n and s['is_feasible']]
        return min(vs,key=lambda s:s['actual_mass_kg']) if vs else None
    def set_n_status(self,*a,**kw):pass
