import pandas as pd
import numpy as np
import os
import glob
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
import time


class ConfigManager:
    """配置管理器，负责加载和管理所有配置文件"""

    def __init__(self, config_dir: str = './setting'):
        self.config_dir = Path(config_dir)
        self.table_schemas = self._load_table_schemas()
        self.table_mappings = self._load_table_mappings()

    def _load_table_schemas(self) -> Dict[str, Any]:
        """加载表结构配置"""
        schema_file = self.config_dir / 'table_schemas.json'
        if not schema_file.exists():
            raise FileNotFoundError(f"表结构文件 {schema_file} 不存在")

        with open(schema_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _load_table_mappings(self) -> Dict[str, Any]:
        """加载表映射配置"""
        mapping_file = self.config_dir / 'table_mapping.json'
        if not mapping_file.exists():
            raise FileNotFoundError(f"表映射文件 {mapping_file} 不存在")

        with open(mapping_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get_table_columns(self, table_name: str) -> List[str]:
        """获取表的列名"""
        return self.table_schemas.get(table_name, {}).get('columns', [])

    def get_primary_key(self, table_name: str) -> List[str]:
        """获取表的主键"""
        return self.table_schemas.get(table_name, {}).get('primary_key', [])

    def get_source_mappings(self, source_table: str) -> Dict[str, Any]:
        """获取源表的字段映射配置"""
        return self.table_mappings.get('source_tables', {}).get(source_table, {})


class DataValidator:
    """数据验证器，负责数据质量检查"""

    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager

    def validate_dataframe(self, df: pd.DataFrame, table_name: str) -> Tuple[bool, List[str]]:
        """验证DataFrame是否符合目标表结构"""
        errors = []
        expected_columns = self.config.get_table_columns(table_name)

        if not expected_columns:
            errors.append(f"未找到表 {table_name} 的列定义")
            return False, errors

        # 检查必要列是否存在（放宽要求，只检查有数据的列）
        missing_columns = set(expected_columns) - set(df.columns)
        if missing_columns and not df.empty:
            # 只有当DataFrame不为空时才报告缺少列的警告
            logging.warning(f"表 {table_name} 缺少列: {missing_columns}")

        # 检查是否有多余列
        extra_columns = set(df.columns) - set(expected_columns)
        if extra_columns:
            logging.warning(f"表 {table_name} 存在多余列: {extra_columns}")

        return True, errors  # 放宽验证，允许部分列缺失

    def validate_primary_key(self, df: pd.DataFrame, table_name: str) -> Tuple[bool, List[str]]:
        """验证主键完整性"""
        errors = []
        warnings = []
        primary_key = self.config.get_primary_key(table_name)

        if not primary_key or df.empty:
            return True, errors

        # 检查主键列是否存在
        missing_pk_columns = set(primary_key) - set(df.columns)
        if missing_pk_columns:
            errors.append(f"缺少主键列: {missing_pk_columns}")
            return False, errors

        # 检查主键是否有空值（改为警告而不是错误）
        for pk_col in primary_key:
            null_count = df[pk_col].isnull().sum()
            if null_count > 0:
                warning_msg = f"主键列 {pk_col} 存在 {null_count} 个空值"
                warnings.append(warning_msg)
                logging.warning(f"表 {table_name}: {warning_msg}")

        # 检查主键是否重复（仅对非空值检查）
        non_null_mask = df[primary_key].notnull().all(axis=1)
        if non_null_mask.sum() > 0:
            duplicate_count = df[non_null_mask].duplicated(subset=primary_key).sum()
            if duplicate_count > 0:
                errors.append(f"主键重复 {duplicate_count} 行")

        return len(errors) == 0, errors

    def validate_data_quality(self, df: pd.DataFrame, table_name: str) -> Tuple[bool, List[str]]:
        """验证数据质量"""
        warnings = []

        if df.empty:
            warnings.append(f"表 {table_name} 无数据")
            return True, warnings

        # 检查空值比例
        for col in df.columns:
            null_ratio = df[col].isnull().sum() / len(df)
            if null_ratio > 0.8:  # 超过80%为空值
                warnings.append(f"列 {col} 空值比例过高: {null_ratio:.2%}")

        # 检查日期字段
        date_columns = [col for col in df.columns if 'time' in col.lower() or 'date' in col.lower()]
        for col in date_columns:
            invalid_dates = 0
            for val in df[col].dropna():
                try:
                    pd.to_datetime(val)
                except:
                    invalid_dates += 1
            if invalid_dates > 0:
                warnings.append(f"日期字段 {col} 包含 {invalid_dates} 个无效日期")

        return True, warnings


class DataTransformer:
    """数据转换器，负责数据类型转换和清理"""

    @staticmethod
    def parse_datetime(value: Any) -> Any:
        """解析日期时间 - 保留源数据"""
        # 取消日期转换逻辑，直接返回原始值
        return value


class FileProcessor:
    """文件处理器，负责读取和识别不同类型的文件"""

    def __init__(self):
        self.supported_extensions = ['.xlsx', '.csv']

    def read_file(self, file_path: str) -> pd.DataFrame:
        """安全读取文件"""
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        if file_path.suffix not in self.supported_extensions:
            raise ValueError(f"不支持的文件格式: {file_path.suffix}")

        try:
            if file_path.suffix == '.xlsx':
                return pd.read_excel(file_path)
            elif file_path.suffix == '.csv':
                return self._read_csv_with_encoding(file_path)
        except Exception as e:
            logging.error(f"读取文件失败 {file_path}: {e}")
            return pd.DataFrame()

    def _read_csv_with_encoding(self, file_path: Path) -> pd.DataFrame:
        """尝试不同编码读取CSV文件"""
        encodings = ['gbk', 'gb2312', 'utf-8', 'latin-1']

        for encoding in encodings:
            try:
                return pd.read_csv(file_path, encoding=encoding, low_memory=False)
            except UnicodeDecodeError:
                continue
            except Exception as e:
                logging.warning(f"使用编码 {encoding} 读取失败: {e}")
                continue

        raise ValueError(f"无法使用任何编码读取文件: {file_path}")

    def identify_file_type(self, file_path: str) -> str:
        """根据文件名识别文件类型"""
        filename = Path(file_path).name.upper()

        # 优先匹配更具体的类型
        if '住院' in filename and '检验' in filename:
            return '住院检验信息'
        elif '门诊' in filename and '检验' in filename:
            return '门诊检验信息'

        # 定义文件类型识别规则
        type_patterns = {
            '病案首页': ['病案首页'],
            '妇科出院': ['妇科出院', '出院'],
            '病理和B超报告': ['病理和B超报告', '病理和B超'],  # 这一行应该放在病理报告前
            '病理报告': ['病理报告', '病理'],
            'B超报告': ['B超报告', 'B超'],
            '病案诊断记录': ['病案诊断记录'],
            # '病案诊断': ['病案诊断', '诊断'],
            'DICOM文件夹': ['DICOM']
        }

        for file_type, patterns in type_patterns.items():
            if any(pattern in filename for pattern in patterns):
                return file_type

        return '未知类型'


class DataMapper:
    """数据映射器，负责根据配置将源数据映射到目标表"""

    def __init__(self, config_manager: ConfigManager, transformer: DataTransformer, id_prefix: str):
        self.config = config_manager
        self.transformer = transformer
        self.id_prefix = id_prefix
        # 用于实时合并的目标表缓存
        self.target_tables_cache = {}
        # ID计数器，用于生成全局唯一ID
        self.id_counters = {}

    def map_data(self, source_df: pd.DataFrame, source_type: str) -> Dict[str, pd.DataFrame]:
        """将源数据映射到目标表，采用实时合并策略"""
        # 获取源表的映射配置
        source_config = self.config.get_source_mappings(source_type)
        if not source_config:
            logging.warning(f"未找到源表 {source_type} 的映射配置")
            return {}

        field_mappings = source_config.get('field_mappings', {})

        # logging.debug(f"源表 {source_type} 的映射配置: {source_config}")
        # logging.debug(f"字段映射详情: {json.dumps(field_mappings, indent=2, ensure_ascii=False)}")

        # 检查源表中是否有多余字段
        unmapped_fields = [col for col in source_df.columns if col not in field_mappings]
        if unmapped_fields:
            logging.info(
                f"源表 {source_type} 中发现 {len(unmapped_fields)} 个未映射字段，将跳过: {unmapped_fields[:10]}{'...' if len(unmapped_fields) > 10 else ''}")

        # 按目标表分组映射配置
        target_tables_mapping = {}
        for source_field, mapping_info in field_mappings.items():
            if source_field not in source_df.columns:
                continue

            if 'target_mappings' not in mapping_info:
                logging.warning(f"字段 {source_field} 缺少target_mappings配置，跳过")
                continue

            for mapping in mapping_info['target_mappings']:
                target_table = mapping.get('target_table')
                target_field = mapping.get('target_field')
                if target_table and target_field:
                    if target_table not in target_tables_mapping:
                        target_tables_mapping[target_table] = {}
                    target_tables_mapping[target_table][target_field] = {
                        'source_field': source_field,
                        'special_handling': mapping.get('special_handling', '')
                    }

        # 初始化目标表缓存
        for target_table in target_tables_mapping.keys():
            if target_table not in self.target_tables_cache:
                target_columns = self.config.get_table_columns(target_table)
                self.target_tables_cache[target_table] = pd.DataFrame(columns=target_columns)

        # 使用新的实时合并映射方法
        for target_table, field_mapping in target_tables_mapping.items():
            self._map_with_realtime_merge(source_df, field_mapping, target_table, source_type)

        # 特殊处理：为病案诊断记录创建chartevents记录 和 omr记录
        if source_type == '病案诊断记录':
            chartevents_df = self._create_chartevents_records(source_df, field_mappings)
            if not chartevents_df.empty:
                self._merge_special_records('chartevents', chartevents_df)
            omr_df = self._create_omr_records(source_df, field_mappings)
            if not omr_df.empty:
                self._merge_special_records('omr', omr_df)

        # # 特殊处理：补全单个诊断信息的seq_num
        # if source_type in ['病案首页', '病案诊断']:
        #
        # 特殊处理：处理多个诊断信息
        # 加入了对单个诊断信息的处理，因为需要额外补全seq_num = 0

        # todo: 这里的特殊处理其实可以合并进入_map_with_realtime_merge
        if source_type in ['妇科出院']:
            diagnoses_df = self._process_multiple_diagnoses(source_df)
            if not diagnoses_df.empty:
                # target_diagnoses_table = 'diagnoses_icd' if source_type == '病案诊断记录' else 'diagnoses_EC'
                target_diagnoses_table = 'diagnoses_EC'
                self._merge_special_records(target_diagnoses_table, diagnoses_df)
# elif target_table == 'd_items' and (source_type == '住院检验信息' or source_type == '门诊检验信息'):
        if source_type in ['住院检验信息', '门诊检验信息']:
            ditems_df = self._create_ditems_records(source_df, field_mappings)
            if not ditems_df.empty:
                self._merge_special_records('d_items', ditems_df)
        # 返回当前缓存的目标表
        return {table: df.copy() for table, df in self.target_tables_cache.items() if not df.empty}

    # type_patterns = {
    #     '病案首页': ['病案首页'],
    #     '妇科出院': ['妇科出院', '出院'],
    #     '病理报告': ['病理报告', '病理'],
    #     '病理和B超报告': ['病理和B超报告', '病理和B超'],
    #     'B超报告': ['B超报告', 'B超'],
    #     '病案诊断记录': ['病案诊断记录'],
    #     # '病案诊断': ['病案诊断', '诊断'],
    #     'DICOM文件夹': ['DICOM']
    # }

    def _map_with_realtime_merge(self, source_df: pd.DataFrame, field_mapping: Dict[str, Dict[str, str]],
                                 target_table: str, source_type: str = ''):
        """使用实时合并策略映射数据"""
        if source_df.empty:
            logging.warning(f"源数据为空，跳过映射到 {target_table}")
            return

        logging.info(f"开始实时合并映射 {len(source_df)} 条记录到目标表 {target_table}")

        # 获取目标表结构和主键信息
        target_columns = self.config.get_table_columns(target_table)
        primary_keys = self.config.get_primary_key(target_table)

        if not target_columns:
            logging.error(f"无法获取目标表 {target_table} 的列结构")
            return

        merged_count = 0
        failed_count = 0

        # 逐行处理源数据
        for idx, source_row in source_df.iterrows():
            try:
                # 创建临时目标行
                temp_target_records = self._create_temp_target_records(source_row, field_mapping, target_table,
                                                                       source_type)

                # 将每个临时目标行合并到缓存中
                for temp_record in temp_target_records:
                    if self._merge_temp_record_to_cache(temp_record, target_table, primary_keys):
                        merged_count += 1
                    else:
                        failed_count += 1

            except Exception as e:
                logging.error(f"处理第 {idx} 行数据时出错: {str(e)}")
                continue

        # 刷新待添加的记录到缓存
        self._flush_pending_records(target_table)

        logging.info(f"目标表 {target_table} 实时合并完成: 合并 {merged_count} 条，失败 {failed_count} 条")

    # 单行数据处理主函数
    def _create_temp_target_records(self, source_row: pd.Series, field_mapping: Dict[str, Dict[str, str]],
                                    target_table: str, source_type: str = '') -> List[Dict]:
        """为单行源数据创建临时目标记录"""
        # 对于会单独处理的情况，应该可以直接跳过
        if target_table == 'chartevents' and source_type == '病案诊断记录':
            return []
        elif target_table == 'omr' and source_type == '病案诊断记录':
            return []
        elif target_table == 'd_items' and (source_type == '住院检验信息' or source_type == '门诊检验信息'):
            return []
        elif target_table == 'diagnoses_EC' and source_type == '妇科出院':  # 妇科出院是单独特殊处理的
            return []
        # # 获取目标表结构
        # target_columns = self.config.get_table_columns(target_table)

        # # 初始化目标记录
        # target_record = {col: None for col in target_columns}
        target_record: dict = {}
        # 不初始化具体的目标记录，直接在映射过程中动态添加字段，有映射关系才会存在这个字段

        # 检查是否有任何有效的映射数据
        has_valid_data = False

        # 映射字段
        for target_field, mapping_info in field_mapping.items():
            source_field = mapping_info['source_field']
            special_handling = mapping_info.get('special_handling', '')
            # if target_table == 'doctor_note':
            #     print(f"映射字段 {source_field} -> {target_field}，特殊处理: {special_handling}")
            if source_field in source_row.index:  # and pd.notna(source_row[source_field]): # 只要字段存在就进行映射，允许空值映射
                source_value = source_row[source_field]
                has_valid_data = True

                # 应用特殊处理
                if special_handling:
                    if "解析参考值范围" in special_handling:
                        range_data = self._parse_reference_range(str(source_value))
                        for range_field, range_value in range_data.items():
                            if range_field in target_columns:
                                target_record[range_field] = range_value
                    # 处理result_name特殊情况, 这里special_handling的内容是此处value对应的result_name的取值,其本身映射取值不变
                    elif "result_name" in special_handling:
                        target_record["result_name"] = self._apply_special_handling(source_value, special_handling)
                        target_record[target_field] = source_value
                    elif "补充seq_num" in special_handling:
                        target_record["seq_num"] = self._apply_special_handling(source_value, special_handling)
                        target_record[target_field] = source_value
                    elif "labevent_id" in special_handling:
                        target_record["labevent_id"] = self.generate_fixed_labevent_id()
                        target_record[target_field] = source_value
                    else:
                        processed_value = self._apply_special_handling(source_value, special_handling)
                        target_record[target_field] = processed_value
                else:
                    target_record[target_field] = source_value

        # 如果没有任何有效数据，直接返回空列表，不创建记录
        if not has_valid_data:
            logging.debug(f"目标表 {target_table}：没有任何有效的映射数据，跳过记录创建")
            return []

        # 处理主键填充
        primary_keys = self.config.get_primary_key(target_table)
        self._fill_missing_primary_keys(target_record, primary_keys, target_table)

        return [target_record]

    def _flush_pending_records(self, target_table: str):
        """将待添加的记录刷新到目标表缓存中"""
        if not hasattr(self, '_pending_records') or target_table not in self._pending_records:
            return

        pending_records = self._pending_records[target_table]
        if not pending_records:
            return

        # 将待添加记录转换为DataFrame
        pending_df = pd.DataFrame(pending_records)

        # 添加到目标表缓存
        if self.target_tables_cache[target_table].empty:
            self.target_tables_cache[target_table] = pending_df
        else:
            self.target_tables_cache[target_table] = pd.concat(
                [self.target_tables_cache[target_table], pending_df],
                ignore_index=True
            )
        # pk = self.config.get_primary_key(target_table)
        # if pk:
        #     self.target_tables_cache[target_table] = self.target_tables_cache[target_table].drop_duplicates(subset=pk,
        #                                                                                                     keep='last')
        # else:
        #     # 对于没有主键的表，进行全行去重（所有列都相同才算重复）
        #     self.target_tables_cache[target_table] = self.target_tables_cache[target_table].drop_duplicates(keep='last')
        # 全行去重
        # self.target_tables_cache[target_table] = self.target_tables_cache[target_table].drop_duplicates(keep='last')

        logging.info(f"刷新 {len(pending_records)} 条待添加记录到目标表 {target_table}")

        # 清空待添加记录
        self._pending_records[target_table] = []

    def _fill_missing_primary_keys(self, target_record: Dict, primary_keys: List[str], target_table: str):
        """填充缺失的主键值

        只有在目标表结构中存在主键，且该主键在映射表中完全不存在的时候，这个主键才需要被填充
        """
        # 定义时间相关的主键名称模式
        time_key_patterns = ['time', 'date', 'created', 'updated', 'modified', 'timestamp', 'datetime']

        for pk in primary_keys:
            if pk not in target_record:
                pk_lower = pk.lower()
                is_time_field = any(pattern in pk_lower for pattern in time_key_patterns)
                if is_time_field:
                    target_record[pk] = None  # 时间相关的主键暂不填充，保留空值
                    continue
                if pk == 'itemid':
                    target_record[pk] = None
                    continue  # itemid字段不填充，保留空值

                if target_table not in self.id_counters:
                    self.id_counters[target_table] = {}
                if pk not in self.id_counters[target_table]:
                    self.id_counters[target_table][pk] = 0
                
                self.id_counters[target_table][pk] += 1
                unique_id = self.id_counters[target_table][pk]
                target_record[pk] = f"{self.id_prefix}{unique_id:08d}"
                # else:
                #     # 其他主键字段使用默认值或时间戳
                #     import time
                #     target_record[pk] = f"auto_{int(time.time() * 1000000) % 1000000}"

    # note: 这里是新增数据合并到当前缓存的地方
    # 每次只合并单行数据
    def _merge_temp_record_to_cache(self, temp_record: Dict, target_table: str, primary_keys: List[str]) -> bool:
        """将临时记录合并到目标表缓存中，返回True表示合并了现有记录，False表示新增记录
        优化版本：使用批量操作减少DataFrame操作次数"""
        # print(f"合并到缓存。。。。\n{temp_record}\n{primary_keys}")
        
        cache_df = self.target_tables_cache[target_table]

        if cache_df.empty or not primary_keys:
            # 缓存为空或无主键，直接添加到缓存
            new_df = pd.DataFrame([temp_record])
            # 确保列顺序和数据类型一致
            if cache_df.empty:
                self.target_tables_cache[target_table] = new_df
            else:
                # 对齐列
                new_df = new_df.reindex(columns=cache_df.columns) # fix: 这里对齐列的时候, [a, b], [a, b, c]， 如果[a, b, c]是后来者，c会丢失，所以再次之前应该保持表结构一致
                self.target_tables_cache[target_table] = pd.concat([cache_df, new_df], ignore_index=True)
            # self.target_tables_cache[target_table] = pd.concat([cache_df, new_df], ignore_index=True)
            return True

        # 检查所有主键字段是否都在temp_record中
        for pk in primary_keys:
            if pk not in temp_record:  # 这里检查的不是值是否为空，而是检查这个主键字段是否在temp_record中存在（即是否有映射关系） 如果没有映射关系，主键应该会被填充(字段或者包括取值)
                # 主键字段缺失
                logging.error(f"临时行主键字段缺失")
                return False
        new_df = pd.DataFrame([temp_record])
        new_df = new_df.reindex(columns=cache_df.columns)
        self.target_tables_cache[target_table] = pd.concat([cache_df, new_df], ignore_index=True)
        return True

    # 这里是合并的逻辑
    def _merge_records(self, cache_df: pd.DataFrame, idx: int, temp_record: Dict):
        """合并两条记录"""
        for col, new_value in temp_record.items():
            if col not in cache_df.columns:
                continue

            old_value = cache_df.at[idx, col]

            # 如果新值为空，保留旧值
            if pd.isna(new_value) or new_value is None:
                continue

            # 如果旧值为空，使用新值
            if pd.isna(old_value) or old_value is None:
                cache_df.at[idx, col] = new_value
                continue

            # 两个值都不为空
            if old_value != new_value:
                # 将值转换为字符串后拼接
                old_str = str(old_value)
                new_str = str(new_value)

                # 如果旧值已经是拼接字符串（包含+）
                if '+' in old_str:
                    # 检查新值是否已经在旧值中
                    parts = old_str.split('+')
                    if new_str not in parts:
                        cache_df.at[idx, col] = old_str + '+' + new_str
                else:
                    # 如果旧值还不是拼接字符串
                    if old_str != new_str:
                        # 转换数据类型为字符串，避免警告
                        if cache_df[col].dtype != 'object':
                            cache_df[col] = cache_df[col].astype('object')
                        cache_df.at[idx, col] = old_str + '+' + new_str

    def _merge_special_records(self, target_table: str, special_df: pd.DataFrame):
        """合并特殊处理生成的记录"""
        # print("merge...2\n")
        if target_table not in self.target_tables_cache:
            target_columns = self.config.get_table_columns(target_table)
            self.target_tables_cache[target_table] = pd.DataFrame(columns=target_columns)

        # 对特殊记录也应用实时合并逻辑
        primary_keys = self.config.get_primary_key(target_table)

        for _, record in special_df.iterrows():
            temp_record = record.to_dict()
            if self._merge_temp_record_to_cache(temp_record, target_table, primary_keys) == False:
                logging.error(f"合并特殊记录到 {target_table} 失败: {temp_record}")


    def generate_fixed_labevent_id(self): # 可优化合并
        # 时间戳部分：14位
        import random
        time_part = datetime.now().strftime("%Y%m%d%H%M%S")
        # 随机数部分：6位（100000-999999）
        random_part = str(random.randint(100000, 999999))
        return time_part + random_part  # 总长度 = 14 + 6 = 20位

    def _create_chartevents_records(self, source_df: pd.DataFrame, field_mappings: Dict) -> pd.DataFrame:
        """为病案诊断记录表创建chartevents记录"""
        chartevents_records = []

        # 定义特定的生命体征字段
        vital_sign_fields = ['SYSTOLIC', 'DIASTOLIC', 'PULSE', 'HEART_RATE', 'HEART_RHYTHM', 'GLU']

        # 找出需要映射到chartevents的字段
        chartevents_fields = {}
        common_fields = {}  # 需要填充到每条记录的公共字段

        logging.debug(f"开始分析chartevents字段映射，源数据列: {list(source_df.columns)}")
        logging.debug(f"field_mappings keys: {list(field_mappings.keys())}")

        for source_field, mapping in field_mappings.items():
            if isinstance(mapping.get('target_mappings'), list):
                for target_mapping in mapping['target_mappings']:
                    if target_mapping.get('target_table') == 'chartevents':
                        if 'itemid' in target_mapping.get('special_handling', ''):
                            # 处理生命体征字段
                            itemid_match = re.search(r'itemid = (\d+)', target_mapping['special_handling'])
                            if itemid_match:
                                chartevents_fields[source_field] = {
                                    'target_field': target_mapping['target_field'],
                                    'itemid': int(itemid_match.group(1))  # 转换为整数
                                }
                                logging.debug(
                                    f"找到生命体征字段映射: {source_field} -> itemid={itemid_match.group(1)}, target_field={target_mapping['target_field']}")
                        elif target_mapping['target_field'] in ['subject_id', 'hadm_id', 'charttime']:
                            # 公共字段（如SICK_ID对应subject_id，RESIDENCE_NO对应hadm_id，OPERATION_DATE对应charttime等）
                            common_fields[target_mapping['target_field']] = source_field
                            logging.debug(f"找到公共字段映射: {source_field} -> {target_mapping['target_field']}")

        logging.info(f"找到的chartevents字段: {chartevents_fields}")
        logging.info(f"找到的公共字段: {common_fields}")

        # 为每行数据创建chartevents记录
        total_rows = len(source_df)
        processed_rows = 0

        for row_idx, row in source_df.iterrows():
            processed_rows += 1
            row_has_valid_vitals = False

            for source_field, field_info in chartevents_fields.items():
                if source_field in source_df.columns:
                    source_value = row[source_field]

                    # 详细的调试日志
                    logging.debug(f"行{row_idx}: 检查字段 {source_field}")
                    logging.debug(f"  原始值: '{source_value}' (类型: {type(source_value)})")
                    logging.debug(f"  pd.notna: {pd.notna(source_value)}")
                    logging.debug(f"  str().strip(): '{str(source_value).strip()}'")
                    logging.debug(
                        f"  条件检查: pd.notna={pd.notna(source_value)}, 非空字符串={str(source_value).strip() != ''}")

                    # 只有当生命体征字段有值时才创建记录
                    if pd.notna(source_value) and str(source_value).strip() != '' and str(
                            source_value).strip().lower() not in ['nan', 'none', 'null']:
                        row_has_valid_vitals = True
                        record = {}

                        # 填充公共字段
                        for target_field, source_field_name in common_fields.items():
                            if source_field_name in source_df.columns and pd.notna(row[source_field_name]) and str(
                                    row[source_field_name]).strip() != '':
                                record[target_field] = row[source_field_name]
                            else:
                                record[target_field] = None

                        # 填充特定字段
                        record['itemid'] = field_info['itemid']
                        record[field_info['target_field']] = str(source_value).strip()

                        chartevents_records.append(record)
                        logging.debug(
                            f"✓ 创建chartevents记录: itemid={record['itemid']}, {field_info['target_field']}={record[field_info['target_field']]}, subject_id={record.get('subject_id', 'N/A')}")
                    else:
                        logging.debug(f"✗ 跳过空值字段 {source_field}: 原始值='{source_value}'")
                else:
                    logging.warning(f"字段 {source_field} 不存在于源数据中")

            # 每处理100行或处理完成时记录进度
            if processed_rows % 100 == 0 or processed_rows == total_rows:
                logging.info(
                    f"已处理 {processed_rows}/{total_rows} 行数据，当前创建了 {len(chartevents_records)} 条chartevents记录")

        if chartevents_records:
            logging.info(f"创建了 {len(chartevents_records)} 条chartevents记录")

        return pd.DataFrame(chartevents_records) if chartevents_records else pd.DataFrame()

# 这里的omr结构不是读取的，而是从数据直接映射得到，结构之所以完整，是因为这个表映射到了omr的每个字段
    def _create_omr_records(self, source_df: pd.DataFrame, field_mappings: Dict) -> pd.DataFrame:
        omr_records = []
        omr_fields = {}
        common_fields = {}

        logging.debug(f"开始分析omr字段映射，源数据列: {list(source_df.columns)}")
        logging.debug(f"field_mappings keys: {list(field_mappings.keys())}")

        for source_field, mapping in field_mappings.items():
            if isinstance(mapping.get('target_mappings'), list):
                for target_mapping in mapping['target_mappings']:
                    if target_mapping.get('target_table') == 'omr':
                        special = target_mapping.get('special_handling', '')
                        if 'result_name' in special:
                            # rn_match = re.search(r"result_name\s*=\s*'([^']+)'", special)
                            # if rn_match:
                            omr_fields[source_field] = {
                                'result_name': self._apply_special_handling(" ", special)
                                # 此时原value并不影响提取值，所以可以传空串
                                # 'result_name': rn_match.group(1)
                            }
                        # elif target_mapping['target_field'] in ['subject_id', 'chartdate']:
                        else:
                            common_fields[target_mapping['target_field']] = source_field
                        logging.debug(f"找到omr字段映射: {source_field} -> {target_mapping['target_field']}, special_handling={special}")

        total_rows = len(source_df)
        processed_rows = 0

        for row_idx, row in source_df.iterrows():
            processed_rows += 1
            for source_field, field_info in omr_fields.items():
                if source_field in source_df.columns:
                    source_value = row[source_field]
                    logging.debug(f"行{row_idx}: 检查字段 {source_field}")
                    logging.debug(f"  原始值: '{source_value}' (类型: {type(source_value)})")
                    logging.debug(f"  pd.notna: {pd.notna(source_value)}")
                    logging.debug(f"  str().strip(): '{str(source_value).strip()}'")
                    logging.debug(
                        f"  条件检查: pd.notna={pd.notna(source_value)}, 非空字符串={str(source_value).strip() != ''}")

                    if pd.notna(source_value) and str(source_value).strip() != '' and str(
                            source_value).strip().lower() not in ['nan', 'none', 'null']:
                        record = {}
                        for target_field, source_field_name in common_fields.items():
                            if source_field_name in source_df.columns and pd.notna(row[source_field_name]) and str(
                                    row[source_field_name]).strip() != '':
                                record[target_field] = row[source_field_name]
                            else:
                                record[target_field] = None
                        record['result_name'] = field_info['result_name']
                        record['result_value'] = str(source_value).strip()
                        omr_records.append(record)
                        logging.debug(
                            f"✓ 创建omr记录: result_name={record['result_name']}, result_value={record['result_value']}, subject_id={record.get('subject_id', 'N/A')}")
                    else:
                        logging.debug(f"✗ 跳过空值字段 {source_field}: 原始值='{source_value}'")
                else:
                    logging.warning(f"字段 {source_field} 不存在于源数据中")

        if omr_records:
            logging.info(f"创建了 {len(omr_records)} 条omr记录")

        return pd.DataFrame(omr_records) if omr_records else pd.DataFrame()

    def _create_ditems_records(self, source_df: pd.DataFrame, field_mappings: Dict) -> pd.DataFrame:
        """为检验信息创建d_items记录"""
        ditems_records = []

        # 获取d_items表的完整结构
        target_columns = self.config.get_table_columns('d_items')
        if not target_columns:
            logging.error("无法获取d_items表的列结构")
            return pd.DataFrame()

        logging.info(f"d_items表结构: {target_columns}")

        # 找出需要映射到d_items的字段分组
        ditems_groups = {}

        logging.debug(f"开始分析d_items字段映射，源数据列: {list(source_df.columns)}")

        for source_field, mapping in field_mappings.items():
            if isinstance(mapping.get('target_mappings'), list):
                for target_mapping in mapping['target_mappings']:
                    if target_mapping.get('target_table') == 'd_items':
                        # 解析special_handling中的分组编号
                        special_handling = target_mapping.get('special_handling', '')
                        group_match = re.search(r'(\d+)', special_handling)
                        group_id = 0  # 默认分组
                        if group_match:
                            group_id = int(group_match.group(1))

                        # 获取目标字段名
                        target_field = target_mapping.get('target_field', '')

                        if group_id not in ditems_groups:
                            ditems_groups[group_id] = {}

                        # 将源字段映射到目标字段
                        ditems_groups[group_id][target_field] = source_field
                        logging.debug(f"找到d_items字段映射: 分组{group_id} - {source_field} -> {target_field}")

        logging.info(f"找到的d_items字段分组: {ditems_groups}")

        if not ditems_groups:
            logging.warning("没有找到映射到d_items表的字段")
            return pd.DataFrame()

        # 为每行数据创建d_items记录
        total_rows = len(source_df)
        processed_rows = 0

        for row_idx, row in source_df.iterrows():
            processed_rows += 1

            # 为每个分组创建记录
            for group_id, field_mapping in ditems_groups.items():
                # 创建完整的d_items记录，包含所有列
                record = {col: None for col in target_columns}

                # 添加分组标识（用于调试）
                record['_group_id'] = group_id

                # 标记是否有有效数据
                has_valid_data = False

                # 填充映射的字段
                for target_field, source_field in field_mapping.items():
                    if source_field in source_df.columns:
                        source_value = row[source_field]

                        # 检查值是否有效
                        if pd.notna(source_value) and str(source_value).strip() != '' and str(
                                source_value).strip().lower() not in ['nan', 'none', 'null']:
                            if target_field in target_columns:
                                record[target_field] = str(source_value).strip()
                                has_valid_data = True
                                logging.debug(
                                    f"行{row_idx} 分组{group_id}: 设置 {target_field} = '{record[target_field]}'")
                            else:
                                logging.warning(f"目标字段 {target_field} 不在d_items表的列结构中")
                    else:
                        logging.warning(f"源字段 {source_field} 不存在于源数据中")

                # 只有当分组中有有效数据时才创建记录
                if has_valid_data:
                    # 移除调试用的_group_id字段
                    if '_group_id' in record:
                        del record['_group_id']

                    ditems_records.append(record)
                    logging.debug(f"✓ 行{row_idx} 创建d_items记录 (分组{group_id}): {record}")
                else:
                    logging.debug(f"✗ 行{row_idx} 跳过分组{group_id}: 所有字段都为空")

            # 每处理100行或处理完成时记录进度
            if processed_rows % 100 == 0 or processed_rows == total_rows:
                logging.info(
                    f"已处理 {processed_rows}/{total_rows} 行数据，当前创建了 {len(ditems_records)} 条d_items记录")

        if ditems_records:
            # 去重处理：d_items是字典表，相同的记录应该只保留一条
            unique_records = []
            seen_records = set()

            for record in ditems_records:
                # 创建一个可哈希的键（基于所有字段值）
                record_key = tuple(sorted([(k, str(v) if v is not None else '') for k, v in record.items()]))

                if record_key not in seen_records:
                    seen_records.add(record_key)
                    unique_records.append(record)

            logging.info(f"创建了 {len(ditems_records)} 条d_items记录，去重后为 {len(unique_records)} 条")
            ditems_records = unique_records

        # 创建DataFrame，确保列顺序与目标表结构一致
        if ditems_records:
            result_df = pd.DataFrame(ditems_records)
            # 确保包含所有目标列，即使有些列在所有记录中都为空
            result_df = result_df.reindex(columns=target_columns)
            return result_df

        return pd.DataFrame(columns=target_columns)

    def _process_multiple_diagnoses(self, source_df: pd.DataFrame) -> pd.DataFrame:
        """处理多个诊断信息，为每个诊断创建单独的diagnoses_EC记录"""
        diagnosis_records = []

        # 定义诊断字段映射：(诊断代码字段, 诊断名称字段, 状态字段, 序号)
        diagnosis_mappings = [
            ('IN_DIAG_CODE', 'IN_DIAG', 'IN_STATE', '妇科出院-0'),
            ('MAIN_DIAGNOSIS_CODE', 'MAIN_DIAGNOSIS', 'FIRST_STATE', '妇科出院-1'),
            ('SECOND_DIAGNOSIS_CODE', 'SECOND_DIAGNOSIS', 'SECOND_STATE', '妇科出院-2'),
            ('THIRD_DIAGNOSIS_CODE', 'THIRD_DIAGNOSIS', 'THIRD_STATE', '妇科出院-3'),

            ('DIAGNOSIS_CODE', 'DIAGNOSIS_NAME', 'DIAGNOSIS_STATE', '其他-0'),
        ]

        for _, row in source_df.iterrows():
            for code_col, name_col, state_col, seq_num in diagnosis_mappings:
                # 检查诊断代码或诊断名称是否存在且非空
                has_code = code_col in source_df.columns and pd.notna(row[code_col]) and str(row[code_col]).strip()
                has_name = name_col in source_df.columns and pd.notna(row[name_col]) and str(row[name_col]).strip()

                if has_code or has_name:
                    record = {}

                    # 填充基础字段
                    if 'SICK_ID' in source_df.columns:
                        record['subject_id'] = row['SICK_ID']
                    if 'RESIDENCE_NO' in source_df.columns:
                        record['hadm_id'] = row['RESIDENCE_NO']

                    # 填充诊断相关字段
                    if has_code:
                        record['diagnosis_code'] = row[code_col]
                    if has_name:
                        record['diagnosis_name'] = row[name_col]

                    # 填充状态字段
                    if state_col in source_df.columns and pd.notna(row[state_col]):
                        record['state'] = row[state_col]

                    # 设置序号
                    record['seq_num'] = seq_num
                    primary_keys = self.config.get_primary_key('diagnoses_EC')
                    self._fill_missing_primary_keys(record, primary_keys, 'diagnoses_EC')
                    diagnosis_records.append(record)

        return pd.DataFrame(diagnosis_records) if diagnosis_records else pd.DataFrame()

    def _apply_special_handling(self, value: Any, special_handling: str) -> Any:
        """应用特殊处理逻辑"""
        if pd.isna(value):
            return None

        value_str = str(value).strip()

        # 处理访问类型映射
        if "1=门诊，2=住院" in special_handling or "1门诊2住院" in special_handling or "需要映射：1=门诊，2=住院" in special_handling:
            if value_str == '1':
                return 'OUTPATIENT'
            elif value_str == '2':
                return 'INPATIENT'
            return value_str

        # 处理年份提取 - 保留原始日期数据
        if "提取年份" in special_handling:
            # 取消年份提取逻辑，直接返回原始值
            return value_str

        # 处理特定itemid设置
        if "itemid =" in special_handling:
            # 提取itemid值
            import re
            match = re.search(r'itemid\s*=\s*(\d+)', special_handling)
            if match:
                return match.group(1)
            return value_str

        # 处理特定result_name设置
        if "result_name =" in special_handling:
            # 提取result_name值
            import re
            match = re.search(r"result_name\s*=\s*['\"]([^'\"]+)['\"]?", special_handling)
            if match:
                return match.group(1)
            return value_str

        # 提取seq_num值
        if "seq_num =" in special_handling:
            import re
            match = re.search(r"seq_num\s*=\s*([^,\s]+)", special_handling)
            if match:
                return match.group(1)
            return value_str

        # 处理需要确定代码的情况 - 暂时保留原值
        if "需要确定对应到MIMIC-IV的具体代码" in special_handling:
            # 暂时保留原值，等待后续映射表完善
            return value_str

        # 处理别名说明
        if "也称为" in special_handling:
            # 保留原值，别名信息已在映射中体现
            return value_str

        # 处理同时映射说明
        if "同时映射到" in special_handling:
            # 保留原值，多重映射已在target_mappings中处理
            return value_str

        return value_str


class DataIntegrator:
    """数据整合器，负责合并和去重数据"""

    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager

    def integrate_data(self, existing_df: pd.DataFrame, new_df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """整合数据，不合并去重"""
        return pd.concat([existing_df, new_df], ignore_index=True)


class HospitalDataExtractor:
    """医院数据提取器主类"""

    def __init__(self, source_dir: str, output_dir: str, config_dir: str = './setting', load_existing: bool = True):
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.load_existing = load_existing

        # 生成ID前缀：时间戳(10位) + 2位随机数
        import time
        import random
        self.id_prefix = f"{int(time.time())}{random.randint(10, 99)}"

        # 初始化组件
        self.config = ConfigManager(config_dir)
        self.validator = DataValidator(self.config)
        self.transformer = DataTransformer()
        self.file_processor = FileProcessor()
        self.mapper = DataMapper(self.config, self.transformer, self.id_prefix)
        self.integrator = DataIntegrator(self.config)

        # 初始化目标表（如果需要加载已有数据）
        self.target_tables = self._initialize_target_tables()

        # 初始化目标表缓存（用于实时合并）
        self.target_tables_cache = {table_name: pd.DataFrame() for table_name in self.target_tables.keys()}

        # 让mapper使用extractor的缓存
        self.mapper.target_tables_cache = self.target_tables_cache

        # 性能监控
        self.processed_files = 0
        self.total_rows_processed = 0
        self.start_time = None

    def _initialize_target_tables(self) -> Dict[str, pd.DataFrame]:
        """初始化所有目标表，如果启用load_existing则加载已有数据"""
        tables = {}
        for table_name, schema in self.config.table_schemas.items():
            if self.load_existing:
                # 尝试加载已有数据
                existing_data = self._load_existing_table_data(table_name)
                if existing_data is not None:
                    tables[table_name] = existing_data
                    logging.info(f"已加载 {table_name} 表的现有数据，共 {len(existing_data)} 条记录")
                else:
                    tables[table_name] = pd.DataFrame(columns=schema['columns'])
                    logging.info(f"未找到 {table_name} 表的现有数据，创建空表")
            else:
                tables[table_name] = pd.DataFrame(columns=schema['columns'])
        return tables

    def _load_existing_table_data(self, table_name: str) -> Optional[pd.DataFrame]:
        """加载已有的目标表数据"""
        try:
            # 检查是否存在对应的输出文件
            csv_file = self.output_dir / f'{table_name}.csv'
            excel_file = self.output_dir / f'{table_name}.xlsx'

            if csv_file.exists():
                logging.info(f"正在加载 {table_name} 表的CSV数据...")
                df = pd.read_csv(csv_file, encoding='utf-8')
                return df
            elif excel_file.exists():
                logging.info(f"正在加载 {table_name} 表的Excel数据...")
                df = pd.read_excel(excel_file)
                return df
            else:
                return None

        except Exception as e:
            logging.warning(f"加载 {table_name} 表的现有数据失败: {str(e)}")
            return None

    def process_all_files(self, batch_size: int = 50):
        """处理所有源文件"""
        logging.info("开始处理医院数据文件...")
        self.start_time = datetime.now()

        # 查找所有支持的文件
        all_files = []
        for ext in self.file_processor.supported_extensions:
            all_files.extend(self.source_dir.glob(f'*{ext}'))

        total_files = len(all_files)
        if total_files == 0:
            logging.warning("未找到任何数据文件")
            return

        logging.info(f"找到 {total_files} 个文件待处理，批处理大小: {batch_size}")

        processed_count = 0
        error_count = 0

        # 分批处理文件
        for batch_start in range(0, total_files, batch_size):
            batch_end = min(batch_start + batch_size, total_files)
            batch_files = all_files[batch_start:batch_end]

            logging.info(f"处理批次 {batch_start // batch_size + 1}/{(total_files - 1) // batch_size + 1}")

            for i, file_path in enumerate(batch_files, batch_start + 1):
                # 跳过临时文件
                if '~$' in file_path.name:
                    logging.info(f"跳过临时文件: {file_path.name}")
                    continue

                try:
                    logging.info(f"[{i}/{total_files}] 正在处理: {file_path.name}")
                    self._process_single_file(file_path)
                    processed_count += 1
                    self.processed_files += 1
                    logging.info(f"✓ 成功处理: {file_path.name}")

                except MemoryError:
                    logging.error(f"内存不足，跳过文件: {file_path.name}")
                    error_count += 1
                    # 强制垃圾回收
                    import gc
                    gc.collect()

                except Exception as e:
                    error_count += 1
                    logging.error(f"✗ 处理文件 {file_path.name} 时出错: {str(e)}")
                    # 记录详细错误信息
                    logging.debug(f"错误详情: {type(e).__name__}: {str(e)}")

            # 批次完成后进行内存清理
            if batch_end < total_files:
                logging.info(f"批次完成，进行内存清理...")
                import gc
                gc.collect()


        # 所有数据保存于子目录，汇总使用merge.py
        logging.info("所有文件处理完成，数据已保存到各个子目录中")

        logging.info(f"数据提取完成！成功: {processed_count}, 失败: {error_count}")

        # 性能统计
        if self.start_time:
            total_time = datetime.now() - self.start_time
            logging.info(f"总耗时: {total_time}")
            logging.info(f"处理文件数: {self.processed_files}")
            logging.info(f"处理总行数: {self.total_rows_processed}")
            if self.total_rows_processed > 0:
                logging.info(f"平均处理速度: {self.total_rows_processed / total_time.total_seconds():.2f} 行/秒")

    def _process_single_file(self, file_path: Path):
        """处理单个文件"""
        try:
            # 读取文件
            df = self.file_processor.read_file(str(file_path))
            if df.empty:
                logging.warning(f"文件 {file_path.name} 为空或读取失败")
                return

            logging.info(f"文件列名: {list(df.columns)}")

            # 识别文件类型
            file_type = self.file_processor.identify_file_type(str(file_path))
            logging.info(f"识别文件类型: {file_type}")

            # 映射数据到目标表
            mapped_tables = self.mapper.map_data(df, file_type)

            # 收集映射后的数据
            for table_name, mapped_df in mapped_tables.items():
                if table_name in self.target_tables:
                    try:

                        if self.target_tables[table_name].empty:
                            self.target_tables[table_name] = mapped_df.copy()
                        else:
                            self.target_tables[table_name] = pd.concat(
                                [self.target_tables[table_name], mapped_df],
                                ignore_index=True
                            )
                        logging.info(f"已收集数据到表 {table_name}: {len(mapped_df)} 行")

                        # # 处理完单个文件后立即保存结果到子目录（使用统一命名格式）
                        # folder_name = self._generate_folder_name(file_path.stem)
                        # self._save_file_results(folder_name)

                    except Exception as e:
                        logging.error(f"收集表 {table_name} 数据时出错: {str(e)}")
                        continue

            try:
                folder_name = self._generate_folder_name(file_path.stem)
                self._save_file_results(folder_name)
            except Exception as e:
                logging.error(f"保存文件 {file_path.stem} 的结果时出错: {str(e)}")

            # 更新统计信息
            self.total_rows_processed += len(df)

            # 清理内存
            del df
            if 'mapped_tables' in locals():
                del mapped_tables

            # 定期垃圾回收
            if self.processed_files % 10 == 0:
                import gc
                gc.collect()

        except Exception as e:
            logging.error(f"处理文件 {file_path.name} 时出错: {str(e)}")
            raise


    def _save_file_results(self, file_stem: str):
        """保存单个文件的处理结果到子目录"""
        # 创建以文件名命名的子目录
        file_output_dir = self.output_dir / file_stem
        file_output_dir.mkdir(exist_ok=True)

        # 保存当前所有表的数据到子目录
        for table_name, table_data in self.target_tables.items():
            if not table_data.empty:
                output_path = file_output_dir / f"{table_name}.csv"
                try:
                    table_data.to_csv(output_path, index=False, encoding='utf-8-sig')
                    logging.info(f"保存文件 {file_stem} 的表 {table_name}: {len(table_data)} 行到 {output_path}")
                except Exception as e:
                    logging.error(f"保存文件 {file_stem} 的表 {table_name} 时出错: {str(e)}")

        # 清空目标表，为下一个文件做准备
        self.target_tables = self._initialize_target_tables()
        # 清空mapper的缓存
        if hasattr(self.mapper, 'target_tables_cache'):
            self.mapper.target_tables_cache = {}

    def _save_chunk_results(self, file_stem: str, chunk_num: int):
        """保存文件块的处理结果到子目录"""
        # 创建以文件名命名的子目录
        file_output_dir = self.output_dir / file_stem
        file_output_dir.mkdir(exist_ok=True)

        # 刷新所有待添加记录到缓存
        if hasattr(self.mapper, 'target_tables_cache'):
            for table_name in self.mapper.target_tables_cache.keys():
                self.mapper._flush_pending_records(table_name)

        # 将缓存数据同步到目标表
        if hasattr(self.mapper, 'target_tables_cache'):
            for table_name, cache_data in self.mapper.target_tables_cache.items():
                if not cache_data.empty:
                    self.target_tables[table_name] = self.integrator.integrate_data(
                        self.target_tables[table_name], cache_data, table_name
                    )

        # 保存当前所有表的数据到子目录（带chunk编号）
        for table_name, table_data in self.target_tables.items():
            if not table_data.empty:
                output_path = file_output_dir / f"{table_name}_chunk_{chunk_num}.csv"
                try:
                    table_data.to_csv(output_path, index=False, encoding='utf-8-sig')
                    logging.info(
                        f"保存文件 {file_stem} 块 {chunk_num} 的表 {table_name}: {len(table_data)} 行到 {output_path}")
                except Exception as e:
                    logging.error(f"保存文件 {file_stem} 块 {chunk_num} 的表 {table_name} 时出错: {str(e)}")

        # 清空目标表，为下一个块做准备
        self.target_tables = self._initialize_target_tables()
        # 清空mapper的缓存
        if hasattr(self.mapper, 'target_tables_cache'):
            self.mapper.target_tables_cache = {}

    def _generate_folder_name(self, file_stem: str) -> str:
        """
        生成统一格式的文件夹名称
        格式: EXTRACT5_{原文件名}_{时间戳}
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return f"EXTRACT5_{file_stem}_{timestamp}"


def extract_dir(
        source: str,
        output: str | None = None,
        config: str = './setting',
        load_existing: bool = True,
        overwrite: bool = True,
        log_level: str = 'INFO',
        enable_timestamp: int = 1
) -> None:
    """
    Args:
        source: 源数据目录路径（必需）
        output: 输出目录路径，默认为 source + "_output"
        config_dir: 配置文件目录路径
        load_existing: 是否加载已有数据进行增量追加
        overwrite: 是否覆盖模式
        log_level: 日志级别
        enable_timestamp: 时间戳开关

    Raises:
        ValueError: 当source为空字符串时
    """
    if source == '':
        raise ValueError("source参数不能为空字符串")

    # 或者更严格的检查（包括空白字符）
    if not source or source.strip() == '':
        raise ValueError("source参数不能为空或只包含空白字符")

    # 处理output默认值
    if output is None:
        output = source + "_output"

    args = {
        'source': source,
        'output': output,
        'config': config,
        'load_existing': load_existing,
        'overwrite': overwrite,
        'log_level': log_level,
        'enable_timestamp': enable_timestamp
    }

    # 处理输出路径时间戳
    if args.enable_timestamp:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"{args.output}_{timestamp}"
        print(f"时间戳已启用，输出目录: {args.output}")
    else:
        print(f"时间戳已禁用，输出目录: {args.output}")

    # 设置日志级别
    # 创建输出目录（如果不存在）
    os.makedirs(args.output, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('extraction.log', encoding='utf-8'),  # 当前目录
            logging.FileHandler(os.path.join(args.output, 'extraction.log'), encoding='utf-8')  # 输出目录
        ]
    )

    try:
        # 创建提取器并处理文件
        logging.info(f"开始数据提取...")
        logging.info(f"源目录: {args.source}")
        logging.info(f"输出目录: {args.output}")
        logging.info(f"配置目录: {args.config}")

        # # 确定是否加载已有数据
        # load_existing = not args.overwrite if args.overwrite else args.load_existing
        load_existing = False

        extractor = HospitalDataExtractor(args.source, args.output, args.config, load_existing=load_existing)

        if load_existing:
            logging.info("运行模式: 增量追加 - 新数据将追加到已有目标数据")
        else:
            logging.info("运行模式: 覆盖模式 - 将生成全新的目标数据")

        extractor.process_all_files()

        logging.info("数据提取任务完成！")

    except Exception as e:
        logging.error(f"数据提取失败: {str(e)}")
        raise


def main():
    import argparse

    # 时间戳开关：设置为1启用时间戳，设置为0禁用时间戳
    ENABLE_TIMESTAMP = 1  # 修改此值为0可禁用时间戳功能

    parser = argparse.ArgumentParser(description='医院数据提取')
    source_default = r"H:/##study/数据集论文/qqy_0412"
    parser.add_argument('--source', '-s',
                        default=source_default,
                        help='源数据目录路径')
    parser.add_argument('--output', '-o',
                        default=source_default + "_output_v1_1",
                        help='输出目录路径')
    parser.add_argument('--config', '-c',
                        default='./setting',
                        help='配置文件目录路径')
    parser.add_argument('--load-existing',
                        action='store_true',
                        default=True,
                        help='是否加载已有目标数据进行增量追加（默认启用）')
    parser.add_argument('--overwrite',
                        action='store_true',
                        help='覆盖模式：不加载已有数据，直接覆盖输出文件')
    parser.add_argument('--log-level',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        default='INFO',
                        help='日志级别')

    args = parser.parse_args()

    # 处理输出路径时间戳
    if ENABLE_TIMESTAMP:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"{args.output}_{timestamp}"
        print(f"时间戳已启用，输出目录: {args.output}")
    else:
        print(f"时间戳已禁用，输出目录: {args.output}")

    # 设置日志级别
    # 创建输出目录（如果不存在）
    os.makedirs(args.output, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('extraction.log', encoding='utf-8'),  # 当前目录
            logging.FileHandler(os.path.join(args.output, 'extraction.log'), encoding='utf-8')  # 输出目录
        ]
    )

    try:
        # 创建提取器并处理文件
        logging.info(f"开始数据提取...")
        logging.info(f"源目录: {args.source}")
        logging.info(f"输出目录: {args.output}")
        logging.info(f"配置目录: {args.config}")

        # # 确定是否加载已有数据
        # load_existing = not args.overwrite if args.overwrite else args.load_existing
        load_existing = False

        extractor = HospitalDataExtractor(args.source, args.output, args.config, load_existing=load_existing)

        if load_existing:
            logging.info("运行模式: 增量追加 - 新数据将追加到已有目标数据")
        else:
            logging.info("运行模式: 覆盖模式 - 将生成全新的目标数据")

        extractor.process_all_files(500)

        logging.info("数据提取任务完成！")

    except Exception as e:
        logging.error(f"数据提取失败: {str(e)}")
        raise


if __name__ == "__main__":
    main()
