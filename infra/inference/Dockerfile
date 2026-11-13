FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY app.py ${LAMBDA_TASK_ROOT}/
COPY model/best_model.joblib ${LAMBDA_TASK_ROOT}/model/best_model.joblib

CMD ["app.handler"]
