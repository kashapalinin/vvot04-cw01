variable "cloud_id" {
  description = "Идентификатор облака Yandex Cloud."
  type        = string
}

variable "folder_id" {
  description = "Идентификатор каталога Yandex Cloud."
  type        = string
}

variable "bucket_name" {
  description = "Глобально уникальное имя бакета с префиксом vvot04-."
  type        = string

  validation {
    condition     = can(regex("^vvot04-[a-z0-9]([a-z0-9-]{0,54}[a-z0-9])?$", var.bucket_name))
    error_message = "Имя бакета должно начинаться с vvot04-, содержать до 63 строчных букв, цифр и дефисов и заканчиваться буквой или цифрой."
  }
}
