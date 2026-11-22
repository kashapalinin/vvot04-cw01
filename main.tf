locals {
  prefix     = "vvot04-cw01"
  table_name = "archives"
  accounts   = toset(["terraform", "gateway", "trigger", "worker"])
  folder_roles = {
    "terraform-queues" = { account = "terraform", role = "ymq.admin" }
    "gateway-queue"    = { account = "gateway", role = "ymq.writer" }
    "gateway-ydb"      = { account = "gateway", role = "ydb.viewer" }
    "gateway-storage"  = { account = "gateway", role = "storage.viewer" }
    "trigger-queue"    = { account = "trigger", role = "ymq.reader" }
    "trigger-function" = { account = "trigger", role = "functions.functionInvoker" }
    "worker-ydb"       = { account = "worker", role = "ydb.editor" }
    "worker-storage"   = { account = "worker", role = "storage.uploader" }
  }
}

provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
}

resource "yandex_iam_service_account" "account" {
  for_each = local.accounts

  name = "${local.prefix}-${each.key}"
}

resource "yandex_resourcemanager_folder_iam_member" "role" {
  for_each = local.folder_roles

  folder_id = var.folder_id
  role      = each.value.role
  member    = "serviceAccount:${yandex_iam_service_account.account[each.value.account].id}"
}

resource "yandex_iam_service_account_static_access_key" "terraform" {
  service_account_id = yandex_iam_service_account.account["terraform"].id
  description        = "Terraform access to Message Queue"

  depends_on = [
    yandex_resourcemanager_folder_iam_member.role["terraform-queues"],
  ]
}

resource "yandex_message_queue" "failed" {
  name                      = "${local.prefix}-failed"
  message_retention_seconds = 14 * 24 * 60 * 60
  access_key                = yandex_iam_service_account_static_access_key.terraform.access_key
  secret_key                = yandex_iam_service_account_static_access_key.terraform.secret_key
}

resource "yandex_message_queue" "tasks" {
  name                       = "${local.prefix}-tasks"
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 24 * 60 * 60
  access_key                 = yandex_iam_service_account_static_access_key.terraform.access_key
  secret_key                 = yandex_iam_service_account_static_access_key.terraform.secret_key

  redrive_policy = jsonencode({
    deadLetterTargetArn = yandex_message_queue.failed.arn
    maxReceiveCount     = 1
  })
}

resource "yandex_ydb_database_serverless" "archives" {
  name                = "${local.prefix}-ydb"
  deletion_protection = false

  # OLTP is the default workload type for serverless databases.
  serverless_database {
    enable_throttling_rcu_limit = true
    throttling_rcu_limit        = 10
    storage_size_limit          = 1
  }
}

resource "terraform_data" "archives" {
  # A document table is created through YDB Document API after the database exists.
  triggers_replace = {
    endpoint = yandex_ydb_database_serverless.archives.document_api_endpoint
    name     = local.table_name
  }

  provisioner "local-exec" {
    command = "python3 scripts/document_table.py create"
    environment = {
      YDB_ENDPOINT = self.triggers_replace.endpoint
      TABLE_NAME   = self.triggers_replace.name
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "python3 scripts/document_table.py delete"
    environment = {
      YDB_ENDPOINT = self.triggers_replace.endpoint
      TABLE_NAME   = self.triggers_replace.name
    }
  }
}

resource "yandex_storage_bucket" "archives" {
  bucket        = var.bucket_name
  folder_id     = var.folder_id
  max_size      = 10 * 1024 * 1024 * 1024
  force_destroy = true

  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }
}

data "archive_file" "worker" {
  type        = "zip"
  source_file = "${path.module}/function/worker.py"
  output_path = "${path.root}/.terraform/vvot04-cw01-worker.zip"
}

resource "yandex_function" "worker" {
  name               = "${local.prefix}-worker"
  runtime            = "python314"
  entrypoint         = "worker.handler"
  memory             = 256
  execution_timeout  = 600
  service_account_id = yandex_iam_service_account.account["worker"].id
  user_hash          = data.archive_file.worker.output_base64sha256

  content {
    zip_filename = data.archive_file.worker.output_path
  }

  environment = {
    BUCKET_NAME  = yandex_storage_bucket.archives.bucket
    YDB_ENDPOINT = yandex_ydb_database_serverless.archives.document_api_endpoint
    TABLE_NAME   = terraform_data.archives.triggers_replace.name
  }

  depends_on = [
    yandex_resourcemanager_folder_iam_member.role["worker-ydb"],
    yandex_resourcemanager_folder_iam_member.role["worker-storage"],
  ]
}

resource "yandex_function_trigger" "tasks" {
  name = "${local.prefix}-trigger"

  message_queue {
    queue_id           = yandex_message_queue.tasks.arn
    service_account_id = yandex_iam_service_account.account["trigger"].id
    batch_size         = 1
    batch_cutoff       = 0
  }

  function {
    id                 = yandex_function.worker.id
    service_account_id = yandex_iam_service_account.account["trigger"].id
  }

  depends_on = [
    yandex_resourcemanager_folder_iam_member.role["trigger-queue"],
    yandex_resourcemanager_folder_iam_member.role["trigger-function"],
  ]
}

resource "yandex_api_gateway" "archives" {
  name = "${local.prefix}-api"
  spec = templatefile("${path.module}/openapi.yaml", {
    bucket_name        = yandex_storage_bucket.archives.bucket
    database           = yandex_ydb_database_serverless.archives.database_path
    folder_id          = var.folder_id
    queue_url          = yandex_message_queue.tasks.id
    service_account_id = yandex_iam_service_account.account["gateway"].id
    table_name         = terraform_data.archives.triggers_replace.name
  })

  depends_on = [
    yandex_resourcemanager_folder_iam_member.role["gateway-queue"],
    yandex_resourcemanager_folder_iam_member.role["gateway-ydb"],
    yandex_resourcemanager_folder_iam_member.role["gateway-storage"],
  ]
}

resource "terraform_data" "gateway_ready" {
  triggers_replace = yandex_api_gateway.archives.domain

  # A new database can take longer to become reachable through API Gateway.
  provisioner "local-exec" {
    command = "python3 scripts/smoke_test.py --wait-ready \"$API_URL\""
    environment = {
      API_URL = "https://${self.triggers_replace}"
    }
  }

  depends_on = [yandex_function_trigger.tasks]
}
