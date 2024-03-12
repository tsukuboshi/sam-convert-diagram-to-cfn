import base64
import datetime
import json
import logging
import os
from typing import Any, Dict

import boto3
import botocore

logger = logging.getLogger()

s3 = boto3.client("s3")
bedrock_runtime = boto3.client(
    service_name="bedrock-runtime", region_name=os.environ["BEDROCK_REGION"]
)
cfn = boto3.client("cloudformation")


# ハンドラー関数
def lambda_handler(event: Dict[Any, Any], context: Any) -> Dict[str, Any]:
    input_bucket_name = event["Records"][0]["s3"]["bucket"]["name"]
    input_diagram_name = event["Records"][0]["s3"]["object"]["key"]

    current_time = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    tmp_image_path = f"/tmp/{current_time}.png"
    file_download(input_bucket_name, input_diagram_name, tmp_image_path)

    row_content = request_claude(tmp_image_path)
    yaml_content = row_content.strip("`").replace("yaml\n", "").strip()

    file_name = cfn_validate(yaml_content, current_time)
    tmp_yaml = f"/tmp/{file_name}"

    with open(tmp_yaml, "w") as file:
        file.write(yaml_content)

    file_upload(file_name, tmp_yaml)

    return {"statusCode": 200}


# 構成図ダウンロード関数
def file_download(bucket_name: str, file_name: str, tmp_file_path: str) -> None:
    try:
        s3.download_file(bucket_name, file_name, tmp_file_path)
        logger.info("Downloaded file: %s", file_name)
    except botocore.exceptions.ClientError as e:
        logger.error("Bucket Download Error: %s", e)
        raise e


# Claudeへのメッセージリクエスト関数
def request_claude(tmp_image_path: str) -> Any:
    model_id = os.environ["MODEL_ID"]
    prompt_path = os.environ["PROMPT_PATH"]

    system_prompt = """
    \n必ず回答の先頭は"```yaml"、末尾は"```"とし、指示された内容に合致するyamlファイルの内容のみを出力してください。
    \n補足を付与したい場合は、yamlファイルにコメントとして記載してください。
    """

    max_tokens = 4096
    temperature = 0

    with open(prompt_path, "rt") as txt_file:
        complement_prompt = txt_file.read()
    logger.info("Complement Prompt: %s", complement_prompt)

    content_text = f"""
    \n\nHuman:
    \n[質問]
    \n入力されたAWS構成図の詳細情報に基づき、その構成をデプロイするためのCloudFormationテンプレート(YAML形式)を作成してください。
    \nテンプレートには、以下の条件を満たすようにしてください:
    \n- 必要なすべてのリソースとそれらの設定を含める
    \n- リソース間の依存関係や参照を適切に定義する
    \n- 補足情報がある場合は、その内容を厳密に反映する
    \n\n[補足]
    \n{complement_prompt}
    \n\nAssistant:
    """

    with open(tmp_image_path, "rb") as image_file:
        content_image = base64.b64encode(image_file.read()).decode("utf8")

    message = {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": content_image,
                },
            },
            {"type": "text", "text": content_text},
        ],
    }

    messages = [message]

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "system": system_prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
    )

    try:
        response = bedrock_runtime.invoke_model(body=body, modelId=model_id)
        response_body = json.loads(response.get("body").read())

        row_content = response_body["content"][0]["text"]
        logger.info("Content: %s", row_content)

        return row_content
    except botocore.exceptions.ClientError as e:
        logger.error("Claude Request Error: %s", e)
        raise e


# CloudFormationバリデーション関数
def cfn_validate(yaml_content: str, current_time: str) -> str:
    try:
        cfn_res = cfn.validate_template(
            TemplateBody=yaml_content,
        )
        logger.info("CloudFormation: %s", cfn_res)
        file_name = f"{current_time}.yaml"
        return file_name
    except botocore.exceptions.ClientError as e:
        logger.warning("CloudFormation Validation Error: %s", e)
        file_name = f"{current_time}_error.yaml"
        return file_name


# テンプレートアップロード関数
def file_upload(file_name: str, tmp_yaml: str) -> None:
    output_s3_bucket = os.environ["OUTPUT_BUCKET"]
    try:
        s3.upload_file(tmp_yaml, output_s3_bucket, file_name)
        logger.info("Uploaded file: %s", file_name)
    except botocore.exceptions.ClientError as e:
        logger.error("Bucket Upload Error: %s", e)
        raise e
