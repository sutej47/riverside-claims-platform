from airflow.decorators import dag, task
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from datetime import datetime

@dag(
    dag_id="00_smoke_test",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["platform", "smoke_test"]
)
def smoke_test():

    @task
    def check_postgres():
        hook = PostgresHook(postgres_conn_id="postgres_sources")
        count = hook.get_first("SELECT COUNT(*) FROM pg_database;")[0]
        print(f"Postgres connection successful. Number of databases: {count}")
        return count

    @task
    def check_minio():
        hook = S3Hook(aws_conn_id="minio_s3")
        bucket = "smoke-test"

        if not hook.check_for_bucket(bucket):
            hook.create_bucket(bucket_name=bucket)
            print(f"Bucket created : {bucket}")

        hook.load_string(
            string_data="Hello from airflow", 
            key="ping.txt", 
            bucket_name=bucket, 
            replace=True
        )

        content = hook.read_key(key="ping.txt", bucket_name=bucket)
        print(f"MinIO reachable. Read back: {content}")
        return content
    check_minio()
    check_postgres()
smoke_test()