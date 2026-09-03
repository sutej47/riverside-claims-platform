from airflow.decorators import dag, task
from datetime import datetime 

@dag(
    dag_id ="00_hello",
    start_date = datetime(2026,1,1),
    schedule = None,
    catchup = False,
)
def hello_dag():
    @task
    def step_one():
        print("first task is done")
        return "1"
    @task 
    def step_two(value):
        print(f"second task is done. got value fro  first task:{value}")
    step_two(step_one())
hello_dag()    