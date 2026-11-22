# Контрольная работа № 1. Система архивирования

Префикс ресурсов `vvot04-`. [Условие работы](https://docs.itiscl.ru/vvot/2026-2027/cw/cw01/)

## Что сделано

- [`openapi.yaml`](openapi.yaml) описывает четыре маршрута. API Gateway проверяет `name` и `url`, отправляет задания в Message Queue, читает метаданные из YDB и отдаёт ZIP из Object Storage.
- [`function/worker.py`](function/worker.py) получает задание от триггера, скачивает файл по публичному HTTP(S) URL, упаковывает его под заданным именем, сохраняет `UUIDv7.zip` в закрытом бакете и добавляет запись `name`, `url`, `key` в YDB. При исключении сообщение остаётся в очереди и после первой неудачной попытки перенаправляется в очередь необработанных заданий.
- [`main.tf`](main.tf) создаёт шлюз, две очереди, триггер, функцию, базу, бакет, сервисные аккаунты и роли. Terraform вызывает [`scripts/document_table.py`](scripts/document_table.py), чтобы создать документную таблицу `archives` через YDB Document API.

## Развёртывание одним `terraform apply`

Запустите команды из корня репозитория. Нужны Terraform 1.5+, `python3` в `PATH` для создания документной таблицы и IAM-токен с правами на создание ресурсов в каталоге. В примере `yc` берёт идентификаторы из настроенного профиля и выпускает токен. При наличии готового токена и идентификаторов можно задать их напрямую. Входные переменные Terraform — `cloud_id`, `folder_id` и глобально уникальное `bucket_name` с префиксом `vvot04-`.

```bash
export TF_VAR_cloud_id="$(yc config get cloud-id)"
export TF_VAR_folder_id="$(yc config get folder-id)"
export TF_VAR_bucket_name="vvot04-cw01-archives-${TF_VAR_folder_id: -8}"
export YC_TOKEN="$(yc iam create-token)"

terraform init
terraform apply
```

`terraform init` устанавливает провайдеры для этой рабочей папки. Один запуск `terraform apply` создаёт все ресурсы, включая таблицу `archives`, сервисные аккаунты, роли и ключ для управления очередями. При первом запуске Terraform ждёт, пока шлюз успешно ответит на `GET /archives`, и выводит `api_url`. Для уже развёрнутого стенда этот адрес можно снова получить командой `terraform output -raw api_url`.

Для проверки из чистой копии используйте каталог, в котором имена ресурсов `vvot04-cw01-*` свободны. Если проверка будет в том же каталоге, сначала удалите старый учебный стенд из папки с его Terraform-состоянием по инструкции ниже.

## Проверка

```bash
API="$(terraform output -raw api_url)"
SOURCE_URL='https://docs.itiscl.ru/vvot/2026-2027/cw/cw01/'
python3 scripts/smoke_test.py "$API" "$SOURCE_URL"
```

Скрипт проверяет ответы `400` и `404`, отправляет корректное задание и получает `202`. После обработки он проверяет UUIDv7, метаданные и побайтовое совпадение файла внутри ZIP с источником.

Чтобы показать каждый шаг отдельно, отправьте задание и откройте список архивов. Обработка асинхронная, поэтому при необходимости повторите второй запрос через несколько секунд.

```bash
curl -i "$API/tasks" \
  -H 'Content-Type: application/json' \
  --data '{"name":"cw01.html","url":"https://docs.itiscl.ru/vvot/2026-2027/cw/cw01/"}'

curl -fsS "$API/archives" | python3 -m json.tool
```

Скопируйте поле `key` из записи `cw01.html`. По нему можно посмотреть метаданные и скачать архив. Команда `unzip -l` покажет имя файла внутри.

```bash
KEY='<ключ из ответа GET /archives>'
curl -fsS "$API/archives/$KEY" | python3 -m json.tool
curl -fsS "$API/download/$KEY" -o "$KEY"
unzip -l "$KEY"
```

## Удаление после демонстрации

В той же папке и с теми же тремя переменными обновите токен и подтвердите удаление ресурсов контрольной.

```bash
export YC_TOKEN="$(yc iam create-token)"
terraform destroy
terraform state list
```

Если будете публиковать код, добавьте исходники и `.terraform.lock.hcl`. Локальные файлы состояния и файлы переменных исключены через `.gitignore`.
