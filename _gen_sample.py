"""Generate a minimal but VALID sample 1C configuration export under tests/sample_config/.

Run:  python _gen_sample.py
Idempotent: overwrites the files it manages.
"""
from pathlib import Path

ROOT = Path("tests/sample_config")


def w(rel: str, content: str) -> None:
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    print("wrote", p)


# --------------------------------------------------------------------------- root
w("Config.xml", """<?xml version="1.0" encoding="UTF-8"?>
<Configuration>
\t<Name>Тестовая конфигурация</Name>
\t<Synonym>ТестКонфигурация</Synonym>
\t<Comment>Образец экспорта конфигурации 1С для тестирования MCP-сервера</Comment>
</Configuration>
""")

# ------------------------------------------------------------------- catalog: nomenclature
w("Catalogs/Catalog.Номенклатура/Info.xml", """<?xml version="1.0" encoding="UTF-8"?>
<Catalog>
\t<Name>Номенклатура</Name>
\t<Type>Catalog</Type>
\t<Synonym>Номенклатура товаров и услуг</Synonym>
\t<Comment>Справочник номенклатуры: товары и услуги, их цены и поставщики</Comment>
\t<Items>
\t\t<Item>
\t\t\t<Name>Код</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Код товара</Synonym>
\t\t\t<Comment>Уникальный код товара в справочнике</Comment>
\t\t\t<DataItemType>String(10)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>Цена</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Цена за единицу</Synonym>
\t\t\t<Comment>Базовая цена товара без налога</Comment>
\t\t\t<DataItemType>Decimal(15,2)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>Поставщик</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Основной поставщик</Synonym>
\t\t\t<Comment>Ссылка на контрагента-поставщика</Comment>
\t\t\t<DataItemType>Reference(Каталог.Партнеры)</DataItemType>
\t\t</Item>
\t</Items>
</Catalog>
""")

w("Catalogs/Catalog.Номенклатура/ObjectModule.bsl", """&AtServer
ПередЗаписью
Процедура ПередЗаписью(Отказ, РежимЗаписи) Экспорт
\t// Валидация: цена не может быть отрицательной
\tЕсли Цена < 0 Тогда
\t\tОтказ = Истина;
\tКонецЕсли;
\tПроверитьЦену();
КонецПроцедуры

Функция ПолучитьСтоимость(Количество) Экспорт
\t// Возвращает стоимость: цена * количество с округлением
\tТоварЦена = РассчитатьЦену(Номенклатура.Ссылка);
\tВозврат ТоварЦена * Количество;
КонецФункции

Процедура ПроверитьЦену()
\tЕсли Не ЗначениеЗаполнено(Цена) Тогда
\t\tСообщить("Цена не заполнена");
\tКонецЕсли;
КонецПроцедуры
""")

w("Catalogs/Catalog.Номенклатура/ManagerModule.bsl", """&AtServer
Функция НайтиПоКодуТовара(Код) Экспорт
\t// Поиск элемента справочника по коду
\tЗапрос = Новый Запрос;
\tЗапрос.Текст = "ВЫБРАТЬ ПЕРВЫЕ 1 Справочник.Номенклатура.Ссылка ГДЕ Справочник.Номenклатура.Код = &Код";
\tВозврат Запрос.Выполнить().Выбрать().Ссылка;
КонецФункции

Процедура РассчитатьЦену(Ссылка) Экспорт
\t// Базовая цена из регистра ценовой информации
\tЦеноваяИнформация = РегистрыСведений.ЦеноваяИнформация.ПолучитьСрезДанных();
\tВозврат 0;
КонецПроцедуры
""")

# ------------------------------------------------------------------- catalog: price list
w("Catalogs/Catalog.ПрайсЛист/Info.xml", """<?xml version="1.0" encoding="UTF-8"?>
<Catalog>
\t<Name>ПрайсЛист</Name>
\t<Type>Catalog</Type>
\t<Synonym>Прайс-листы</Synonym>
\t<Comment>Справочник прайс-листов с действующими ценами</Comment>
\t<Items>
\t\t<Item>
\t\t\t<Name>Номенклатура</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Товар</Synonym>
\t\t\t<Comment>Товар из справочника номенклатуры</Comment>
\t\t\t<DataItemType>Reference(Каталог.Номенклатура)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>Цена</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Цена в прайсе</Synonym>
\t\t\t<Comment>Цена товара в данном прайс-листе</Comment>
\t\t\t<DataItemType>Decimal(15,2)</DataItemType>
\t\t</Item>
\t</Items>
</Catalog>
""")

# ------------------------------------------------------------------- document: realization
w("Documents/Document.Реализация/Info.xml", """<?xml version="1.0" encoding="UTF-8"?>
<Document>
\t<Name>Реализация</Name>
\t<Type>Document</Type>
\t<Synonym>Реализация товаров</Synonym>
\t<Comment>Документ продажи (реализации) товаров покупателю</Comment>
\t<Items>
\t\t<Item>
\t\t\t<Name>Покупатель</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Контрагент-покупатель</Synonym>
\t\t\t<Comment>Кому реализованы товары</Comment>
\t\t\t<DataItemType>Reference(Каталог.Партнеры)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>Товары</Name>
\t\t\t<Type>TabularSection</Type>
\t\t\t<Synonym>Реализованные товары</Synonym>
\t\t\t<Comment>Табличная часть: позиции реализованных товаров</Comment>
\t\t\t<Items>
\t\t\t\t<Item>
\t\t\t\t\t<Name>Номенклатура</Name>
\t\t\t\t\t<Type>Attribute</Type>
\t\t\t\t\t<Synonym>Товар</Synonym>
\t\t\t\t\t<DataItemType>Reference(Каталог.Номенклатуra)</DataItemType>
\t\t\t\t</Item>
\t\t\t\t<Item>
\t\t\t\t\t<Name>Количество</Name>
\t\t\t\t\t<Type>Attribute</Type>
\t\t\t\t\t<Synonym>Кол-во</Synonym>
\t\t\t\t\t<DataItemType>Decimal(15,3)</DataItemType>
\t\t\t\t</Item>
\t\t\t\t<Item>
\t\t\t\t\t<Name>Сумма</Name>
\t\t\t\t\t<Type>Attribute</Type>
\t\t\t\t\t<Synonym>Сумма позиции</Synonym>
\t\t\t\t\t<DataItemType>Decimal(15,2)</DataItemType>
\t\t\t\t</Item>
\t\t\t</Items>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>ИТОГОСумма</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Итого по документу</Synonym>
\t\t\t<Comment>Общая сумма документа</Comment>
\t\t\t<DataItemType>Decimal(15,2)</DataItemType>
\t\t</Item>
\t</Items>
</Document>
""")

w("Documents/Document.Реализация/ObjectModule.bsl", """&AtServer
ПриЗаписи
Процедура ПриЗаписи() Экспорт
\t// Расчёт итоговой суммы документа при записи
\tРассчитатьСуммуРеализации();
КонецПроцедуры

Процедура РассчитатьСуммуРеализации() Экспорт
\t// Считает сумму каждого товара и итог документа
\tИтого = 0;
\tДля Каждого Строка Из Товары Цикл
\t\tСтрока.Сумма = ПолучитьСтоимость(Строка.Количество);
\t\tИтого = Итого + Строка.Сумма;
\tКонецЦикла;
\tИТОГОСумма = Итого;
\tЗаписатьОбороты(Покупатель, Итого);
КонецПроцедуры

Процедура ЗаписатьОбороты(Контрагент, Сумма)
\t// Регистрирует оборот по реализации в регистре
\tЗапись = Новый ЗаписьРегистрации(РегистрыНакопления.Обороты, РегистрыНакопления.Операция.Реализация);
\tЗапись.Период = Дата;
\tЗаписать(Запись);
КонецПроцедуры
""")

# ------------------------------------------------------------------- constant: company info
w("Constants/Constant.ИнформацияОКомпании/Info.xml", """<?xml version="1.0" encoding="UTF-8"?>
<Constant>
\t<Name>ИнформацияОКомпании</Name>
\t<Type>Constant</Type>
\t<Synonym>Информация о компании</Synonym>
\t<Comment>Константа с реквизитами организации (название, ИНН)</Comment>
\t<Items>
\t\t<Item>
\t\t\t<Name>Название</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Наименование организации</Synonym>
\t\t\t<DataItemType>String(200)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>ИНН</Name>
\t\t\t<Type>Attribute</Type>
\t\t\t<Synonym>Идентификационный номер</Synonym>
\t\t\t<DataItemType>String(12)</DataItemType>
\t\t</Item>
\t</Items>
</Constant>
""")

# ------------------------------------------------------------------- accumulation register: turnovers
w("Registers/AccumulationRegisters/Register.Обороты/Info.xml", """<?xml version="1.0" encoding="UTF-8"?>
<AccumulationRegister>
\t<Name>Обороты</Name>
\t<Type>AccumulationRegister</Type>
\t<Synonym>Обороты по реализации</Synonym>
\t<Comment>Регистр накопления: обороты по контрагентам и суммам</Comment>
\t<Items>
\t\t<Item>
\t\t\t<Name>Контрагент</Name>
\t\t\t<Type>Resource</Type>
\t\t\t<Synonym>Покупатель</Synonym>
\t\t\t<DataItemType>Reference(Каталог.Партнеры)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>Сумма</Name>
\t\t\t<Type>Resource</Type>
\t\t\t<Synonym>Оборотная сумма</Synonym>
\t\t\t<DataItemType>Decimal(15,2)</DataItemType>
\t\t</Item>
\t</Items>
</AccumulationRegister>
""")

# ------------------------------------------------------------------- information register: price info
w("Registers/InformationRegisters/Register.ЦеноваяИнформация/Info.xml", """<?xml version="1.0" encoding="UTF-8"?>
<InformationRegister>
\t<Name>ЦеноваяИнформация</Name>
\t<Type>InformationRegister</Type>
\t<Synonym>Ценовая информация</Synonym>
\t<Comment>Регистр сведений: актуальные цены на номенклатуру</Comment>
\t<Items>
\t\t<Item>
\t\t\t<Name>Номенклатура</Name>
\t\t\t<Type>Dimension</Type>
\t\t\t<Synonym>Товар</Synonym>
\t\t\t<DataItemType>Reference(Каталог.Номенклатуra)</DataItemType>
\t\t</Item>
\t\t<Item>
\t\t\t<Name>Цена</Name>
\t\t\t<Type>Resource</Type>
\t\t\t<Synonym>Актуальная цена</Synonym>
\t\t\t<DataItemType>Decimal(15,2)</DataItemType>
\t\t</Item>
\t</Items>
</InformationRegister>
""")

print("done")
