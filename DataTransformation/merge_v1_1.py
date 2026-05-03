#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge.py - 数据合并工具

功能：
1. 合并指定目录下所有子目录内的CSV数据文件
2. 对主键进行查重与合并
3. 同一字段在多个记录中都有值时用+连接
4. 生成完整的合并数据到指定目录

"""

import pandas as pd
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set
import argparse
from collections import defaultdict
import sys
import json
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('merge5.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

class DataMerger:
    """数据合并器类"""
    
    def __init__(self, source_dir: str, output_dir: str = None, memory_optimization: bool = False):
        """
        初始化数据合并器
        
        Args:
            source_dir: 源数据目录路径
            output_dir: 输出目录路径，默认为源目录
            memory_optimization: 是否启用内存优化（每处理一个文件夹后清空内存）
        """
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir) if output_dir else self.source_dir
        
        # 验证目录存在
        if not self.source_dir.exists():
            raise ValueError(f"源目录不存在: {self.source_dir}")
        
        # 创建输出目录
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 内存优化开关
        self.memory_optimization = memory_optimization
        
        # 存储合并后的数据
        self.merged_tables: Dict[str, pd.DataFrame] = {}
        
        # 合并记录文件路径
        self.merge_record_file = self.output_dir / 'merge_record.json'
        
        # 加载已合并记录
        self.merged_folders = self._load_merge_record()
        
        # 从配置文件加载主键配置
        self.primary_keys = self._load_primary_keys()
        
        logging.info(f"初始化数据合并器 - 源目录: {self.source_dir}, 输出目录: {self.output_dir}")
        logging.info(f"内存优化: {'启用' if self.memory_optimization else '禁用'}")
    
    def _get_table_columns(self, table_name: str) -> List[str]:
        """
        获取表的完整列定义
        
        Args:
            table_name: 表名
            
        Returns:
            列名列表
        """
        columns = []
        # 尝试从table_schemas.json获取完整列信息
        schema_file = Path(__file__).parent / 'setting' / 'table_schemas.json'
        if schema_file.exists():
            try:
                with open(schema_file, 'r', encoding='utf-8') as f:
                    schema_data = json.load(f)
                    if table_name in schema_data and 'columns' in schema_data[table_name]:
                        columns = schema_data[table_name]['columns']
            except Exception as e:
                logging.warning(f"读取表 {table_name} 的schema配置失败: {str(e)}")
        
        # 如果没有找到完整列信息，使用主键作为基础列
        if not columns and table_name in self.primary_keys:
            columns = self.primary_keys[table_name]
        
        return columns
    
    def _validate_table_structure(self, table_name: str, csv_path: Path) -> bool:
        """
        验证现有表结构是否与配置一致
        
        Args:
            table_name: 表名
            csv_path: CSV文件路径
            
        Returns:
            True如果结构一致，False如果不一致
            
        Raises:
            ValueError: 如果表结构不匹配
        """
        expected_columns = self._get_table_columns(table_name)
        if not expected_columns:
            logging.warning(f"表 {table_name} 没有找到列定义，跳过结构验证")
            return True
        
        try:
            # 读取现有文件的列名（只读第一行）
            existing_df = pd.read_csv(csv_path, nrows=0, encoding='utf-8-sig')
            existing_columns = list(existing_df.columns)
            
            # 检查列是否一致
            if set(existing_columns) != set(expected_columns):
                missing_cols = set(expected_columns) - set(existing_columns)
                extra_cols = set(existing_columns) - set(expected_columns)
                
                error_msg = f"表 {table_name} 结构不匹配:\n"
                if missing_cols:
                    error_msg += f"  缺少列: {missing_cols}\n"
                if extra_cols:
                    error_msg += f"  多余列: {extra_cols}\n"
                error_msg += f"  期望列: {expected_columns}\n"
                error_msg += f"  实际列: {existing_columns}"
                
                raise ValueError(error_msg)
            
            logging.info(f"表 {table_name} 结构验证通过: {len(expected_columns)} 列")
            return True
            
        except pd.errors.EmptyDataError:
            # 空文件，需要重新创建
            logging.info(f"表 {table_name} 为空文件，将重新创建结构")
            return False
        except Exception as e:
            logging.error(f"验证表 {table_name} 结构时出错: {str(e)}")
            raise
    
    def initialize_table_structures(self):
        """
        初始化所有表结构
        在运行开始时检查目标目录下的表结构，如果不存在则创建，如果存在则验证
        
        Raises:
            ValueError: 如果现有表结构与配置不匹配
        """
        logging.info("开始初始化表结构...")
        
        # 获取所有配置的表
        all_tables = set(self.primary_keys.keys())
        
        for table_name in all_tables:
            output_path = self.output_dir / f"{table_name}.csv"
            
            if output_path.exists():
                # 文件存在，验证结构
                try:
                    self._validate_table_structure(table_name, output_path)
                    logging.info(f"表 {table_name} 已存在且结构正确")
                except ValueError as e:
                    logging.error(f"表结构验证失败: {str(e)}")
                    raise
                except Exception as e:
                    # 如果验证失败（如文件损坏），重新创建
                    logging.warning(f"表 {table_name} 验证出错，将重新创建: {str(e)}")
                    self._create_empty_table(table_name, output_path)
            else:
                # 文件不存在，创建空表结构
                self._create_empty_table(table_name, output_path)
        
        logging.info(f"表结构初始化完成，共处理 {len(all_tables)} 个表")
    
    def _create_empty_table(self, table_name: str, output_path: Path):
        """
        创建空表结构
        
        Args:
            table_name: 表名
            output_path: 输出文件路径
        """
        columns = self._get_table_columns(table_name)
        if columns:
            empty_df = pd.DataFrame(columns=columns)
            empty_df.to_csv(output_path, index=False, encoding='utf-8-sig')
            logging.info(f"创建空表 {table_name}: 0 行，{len(columns)} 列到 {output_path}")
        else:
            logging.warning(f"表 {table_name} 没有找到列定义，跳过创建")
    
    def _load_primary_keys(self) -> Dict[str, List[str]]:
        """
        从table_schemas.json文件加载主键配置
        
        Returns:
            主键配置字典
        
        Raises:
            FileNotFoundError: 配置文件不存在
            ValueError: 配置文件格式错误或未找到主键配置
        """
        # 配置文件路径
        schema_file = Path(__file__).parent / 'setting' / 'table_schemas.json'
        
        if not schema_file.exists():
            error_msg = f"主键配置文件不存在: {schema_file}"
            logging.error(error_msg)
            raise FileNotFoundError(error_msg)
        
        try:
            with open(schema_file, 'r', encoding='utf-8') as f:
                schema_data = json.load(f)
        except Exception as e:
            error_msg = f"读取主键配置文件失败: {str(e)}"
            logging.error(error_msg)
            raise ValueError(error_msg)
        
        # 提取主键配置
        primary_keys = {}
        for table_name, table_info in schema_data.items():
            if isinstance(table_info, dict) and 'primary_key' in table_info:
                primary_keys[table_name] = table_info['primary_key']
            elif isinstance(table_info, dict) and 'primary_keys' in table_info:
                primary_keys[table_name] = table_info['primary_keys']
        
        if not primary_keys:
            error_msg = "配置文件中未找到任何主键配置"
            logging.error(error_msg)
            raise ValueError(error_msg)
        
        logging.info(f"从配置文件加载主键配置: {len(primary_keys)} 个表")
        return primary_keys
    
    def _load_merge_record(self) -> Set[str]:
        """
        加载已合并的文件夹记录
        
        Returns:
            已合并的文件夹名称集合
        """
        if self.merge_record_file.exists():
            try:
                with open(self.merge_record_file, 'r', encoding='utf-8') as f:
                    record_data = json.load(f)
                    merged_folders = set(record_data.get('merged_folders', []))
                    logging.info(f"加载合并记录: {len(merged_folders)} 个已合并文件夹")
                    return merged_folders
            except Exception as e:
                logging.error(f"加载合并记录失败: {str(e)}")
        return set()
    
    def _save_merge_record(self):
        """
        保存合并记录到文件
        """
        try:
            record_data = {
                'last_updated': datetime.now().isoformat(),
                'merged_folders': list(self.merged_folders),
                'total_merged': len(self.merged_folders)
            }
            with open(self.merge_record_file, 'w', encoding='utf-8') as f:
                json.dump(record_data, f, ensure_ascii=False, indent=2)
            logging.info(f"保存合并记录: {len(self.merged_folders)} 个文件夹")
        except Exception as e:
            logging.error(f"保存合并记录失败: {str(e)}")
    
    def find_folders_with_csv_files(self) -> Dict[str, Dict[str, List[Path]]]:
        """
        查找所有子目录中的CSV文件，按文件夹组织
        
        Returns:
            Dict[文件夹名, Dict[表名, List[文件路径]]]
        """
        folders_data = {}
        
        # 遍历所有子目录
        for subdir in self.source_dir.iterdir():
            if subdir.is_dir():
                # 检查是否已经合并过此文件夹
                if subdir.name in self.merged_folders:
                    logging.info(f"跳过已合并的文件夹: {subdir.name}")
                    continue
                
                # 查找子目录中的CSV文件
                folder_csv_files = defaultdict(list)
                valid_files_found = False
                
                for csv_file in subdir.glob('*.csv'):
                    table_name = self._extract_table_name(csv_file.name)
                    if table_name:
                        folder_csv_files[table_name].append(csv_file)
                        valid_files_found = True
                
                # 只有找到有效文件的文件夹才记录
                if valid_files_found:
                    folders_data[subdir.name] = dict(folder_csv_files)
                    logging.info(f"发现文件夹: {subdir.name}，包含 {len(folder_csv_files)} 个表")
                else:
                    # 如果文件夹中没有有效的CSV文件，记录但不处理
                    csv_count = len(list(subdir.glob('*.csv')))
                    if csv_count > 0:
                        logging.info(f"跳过文件夹 {subdir.name}: 包含 {csv_count} 个CSV文件，但无匹配的表名")
                    else:
                        logging.debug(f"跳过文件夹 {subdir.name}: 不包含CSV文件")
        
        return folders_data
    
    def _load_existing_data(self) -> Dict[str, pd.DataFrame]:
        """
        从目标目录加载现有数据（用于内存优化模式）
        
        Returns:
            Dict[表名, DataFrame]
        """
        existing_data = {}
        
        for table_name in self.primary_keys.keys():
            output_path = self.output_dir / f"{table_name}.csv"
            if output_path.exists():
                try:
                    # 读取现有数据，强制所有列为字符串类型
                    df = pd.read_csv(output_path, dtype=str)
                    if not df.empty:
                        existing_data[table_name] = df
                        logging.debug(f"加载现有数据: {table_name}, {len(df)} 行")
                    else:
                        existing_data[table_name] = df  # 即使是空表也加载，以保持结构一致
                        logging.debug(f"表 {table_name} 文件存在但为空")
                except Exception as e:
                    logging.warning(f"读取现有数据失败 {table_name}: {str(e)}")
            else:
                logging.debug(f"表 {table_name} 文件不存在")
        
        return existing_data
    
    def _extract_table_name(self, filename: str) -> Optional[str]:
        """
        从文件名中提取表名
        
        Args:
            filename: 文件名
            
        Returns:
            表名或None
        """
        # 移除.csv后缀
        name = filename.replace('.csv', '')
        
        # 移除chunk后缀（如果有）
        if '_chunk_' in name:
            name = name.split('_chunk_')[0]
        
        # 检查是否为已知表名
        if name in self.primary_keys:
            return name
        
        return None
    
    def process_folder_by_folder(self):
        """
        逐个文件夹处理数据（新的处理流程）
        """
        logging.info("开始逐个文件夹处理数据...")
        
        # 查找所有待处理的文件夹
        folders_data = self.find_folders_with_csv_files()
        
        if not folders_data:
            logging.warning("未找到任何待处理的文件夹")
            return
        
        logging.info(f"发现 {len(folders_data)} 个待处理文件夹")
        
        # 根据内存优化模式选择不同的处理策略
        if not self.memory_optimization:
            # 非内存优化模式：开始时读取所有旧数据到内存
            logging.info("非内存优化模式：加载所有现有数据到内存...")
            self.merged_tables = self._load_existing_data()  # ???
        
        # 逐个处理文件夹
        for folder_name, folder_csv_files in folders_data.items():
            try:
                if self.memory_optimization:
                    # 内存优化模式：每次处理前读取当前文件夹涉及的表
                    logging.info(f"内存优化模式：加载文件夹 {folder_name} 涉及的现有数据...")
                    involved_tables = set(folder_csv_files.keys())
                    self.merged_tables = self._load_specific_tables(involved_tables)
                
                logging.info(f"开始处理文件夹: {folder_name}")
                
                # 处理当前文件夹中的所有表
                folder_processed = False
                for table_name, file_list in folder_csv_files.items():
                    logging.info(f"处理表 {table_name}，文件数量: {len(file_list)}")
                    
                    # 合并当前表的文件
                    # merged_data = self._merge_table_files(table_name, file_list)
                    
                    # 直接加载并合并当前表的文件（不在此处进行主键合并，后续统一处理）
                    merged_data = self._load_table_files(table_name, file_list)

                    if merged_data is not None and not merged_data.empty:
                        # 如果内存中已有该表数据，需要合并
                        if table_name in self.merged_tables: # 确保目标表结构中存在这个表，现在空表也会加载以确保结构一致
                            logging.info(f"合并表 {table_name} 的新旧数据...")
                            # 合并新旧数据
                            combined_data = pd.concat([self.merged_tables[table_name], merged_data], ignore_index=True)
                            
                            primary_key_cols = self.primary_keys.get(table_name, [])
                            if primary_key_cols:
                                # print(f"！！！！！！！！！！！！！！！！！正在按主键 {primary_key_cols} 合并数据，初始行数: {len(combined_data)}")
                                combined_data = self._merge_by_primary_key(combined_data, primary_key_cols)

                            
                            self.merged_tables[table_name] = combined_data
                        else:
                            self.merged_tables[table_name] = merged_data
                        
                        logging.info(f"表 {table_name} 处理完成，当前总行数: {len(self.merged_tables[table_name])}")
                        folder_processed = True
                    else:
                        logging.warning(f"表 {table_name} 合并后为空")
                
                # 如果文件夹有数据被处理，保存结果并标记为已处理
                if folder_processed:
                    if self.memory_optimization:
                        # 内存优化模式：重写涉及的表文件，然后清空内存
                        logging.info(f"内存优化模式：重写文件夹 {folder_name} 涉及的表文件...")
                        self._rewrite_involved_tables(set(folder_csv_files.keys()))
                        self.merged_tables.clear()
                    else:
                        # 非内存优化模式：同步涉及的表到文件
                        logging.info(f"非内存优化模式：同步文件夹 {folder_name} 涉及的表...")
                        self._sync_involved_tables(set(folder_csv_files.keys()))
                    
                    # 标记文件夹为已处理
                    self.merged_folders.add(folder_name)
                    self._save_merge_record()
                    
                    logging.info(f"文件夹 {folder_name} 处理完成")
                else:
                    logging.warning(f"文件夹 {folder_name} 未产生有效数据，跳过")
                    
            except Exception as e:
                logging.error(f"处理文件夹 {folder_name} 时出错: {str(e)}")
                # 继续处理下一个文件夹
                continue
        
        logging.info("所有文件夹处理完成")

# 读取并合并列表中的所有数据
    def _load_table_files(self, table_name: str, file_list: List[Path]) -> Optional[pd.DataFrame]:
        """
        合并单个表的所有文件
        
        Args:
            table_name: 表名
            file_list: 文件列表
            
        Returns:
            合并后的DataFrame
        """
        # print(f"_merge_table_files: {table_name}, 文件数量: {len(file_list)}")
        if not file_list:
            return None

        # 单个文件也进行合并操作
        logging.info(f"表 {table_name} 有 {len(file_list)} 个文件需要合并")
        
        # 读取所有文件数据，强制所有列为字符串类型
        all_data = []
        for file_path in file_list:
            try:
                df = pd.read_csv(file_path, encoding='utf-8-sig', dtype=str)
                if not df.empty:
                    all_data.append(df)
                    logging.debug(f"读取文件 {file_path.name}: {len(df)} 行")
            except Exception as e:
                logging.error(f"读取文件 {file_path} 时出错: {str(e)}")
                continue
        
        if not all_data:
            return None
        
        # 合并所有数据
        combined_data = pd.concat(all_data, ignore_index=True)
        
        return combined_data  

        
    def _load_specific_tables(self, table_names: Set[str]) -> Dict[str, pd.DataFrame]:
        """
        加载指定的表数据（内存优化模式使用）
        
        Args:
            table_names: 需要加载的表名集合
            
        Returns:
            Dict[表名, DataFrame]
        """
        loaded_tables = {}
        
        for table_name in table_names:
            output_path = self.output_dir / f"{table_name}.csv"
            
            if output_path.exists():
                try:
                    # 读取现有数据，强制所有列为字符串类型
                    df = pd.read_csv(output_path, encoding='utf-8-sig', dtype=str)
                    if not df.empty:
                        loaded_tables[table_name] = df
                        logging.info(f"加载现有表 {table_name}，行数: {len(df)}")
                    else:
                        loaded_tables[table_name] = df  # 即使是空表也加载，以保持结构一致
                        logging.info(f"表 {table_name} 文件为空")
                except Exception as e:
                    logging.error(f"加载表 {table_name} 失败: {e}")
            else:
                logging.info(f"表 {table_name} 文件不存在，将创建新表")
        
        return loaded_tables

    def _rewrite_involved_tables(self, table_names: Set[str]):
        """
        重写涉及的表文件（内存优化模式使用）
        
        Args:
            table_names: 需要重写的表名集合
        """
        for table_name in table_names:
            if table_name in self.merged_tables:
                output_path = self.output_dir / f"{table_name}.csv"
                try:
                    self.merged_tables[table_name].to_csv(output_path, index=False, encoding='utf-8-sig')
                    logging.info(f"重写表文件 {table_name}，行数: {len(self.merged_tables[table_name])}")
                except Exception as e:
                    logging.error(f"重写表文件 {table_name} 失败: {e}")

    def _sync_involved_tables(self, table_names: Set[str]):
        """
        同步涉及的表到文件（非内存优化模式使用）
        
        Args:
            table_names: 需要同步的表名集合
        """
        for table_name in table_names:
            if table_name in self.merged_tables:
                output_path = self.output_dir / f"{table_name}.csv"
                try:
                    self.merged_tables[table_name].to_csv(output_path, index=False, encoding='utf-8-sig')
                    logging.info(f"同步表文件 {table_name}，行数: {len(self.merged_tables[table_name])}")
                except Exception as e:
                    logging.error(f"同步表文件 {table_name} 失败: {e}")

    def _merge_to_target_file(self, table_name: str, new_data: pd.DataFrame):
        """
        直接将新数据合并到目标文件（已废弃，使用新的同步逻辑）
        """
        pass

    def _save_folder_data(self):
        """
        保存当前内存中的数据到文件
        """
        for table_name, data in self.merged_tables.items():
            if data is not None and not data.empty:
                output_path = self.output_dir / f"{table_name}.csv"
                
                try:
                    # 确保列顺序正确
                    expected_columns = self._get_table_columns(table_name)
                    
                    # 检查并补充缺失的列
                    for col in expected_columns:
                        if col not in data.columns:
                            data[col] = None
                    
                    # 按预期顺序重排列
                    data = data[expected_columns]
                    
                    # 保存到文件
                    data.to_csv(output_path, index=False, encoding='utf-8')
                    logging.info(f"保存表 {table_name} 到 {output_path}，共 {len(data)} 行")
                    
                except Exception as e:
                    logging.error(f"保存表 {table_name} 时出错: {str(e)}")
            else:
                logging.debug(f"跳过空表: {table_name}")
    
    def find_all_csv_files(self) -> Dict[str, List[Path]]:
        """
        查找所有子目录中的CSV文件（旧版本方法，保留作为备用）
        
        Returns:
            Dict[表名, List[文件路径]]
        """
        csv_files = defaultdict(list)
        
        # 遍历所有子目录
        for subdir in self.source_dir.iterdir():
            if subdir.is_dir():
                # 检查是否已经合并过此文件夹
                if subdir.name in self.merged_folders:
                    logging.info(f"跳过已合并的文件夹: {subdir.name}")
                    continue
                
                # 查找子目录中的CSV文件，并检查是否有有效文件
                valid_files_found = False
                for csv_file in subdir.glob('*.csv'):
                    table_name = self._extract_table_name(csv_file.name)
                    if table_name:
                        csv_files[table_name].append(csv_file)
                        valid_files_found = True
                
                # 只有找到有效文件的文件夹才记录为已处理
                if valid_files_found:
                    self.merged_folders.add(subdir.name)
                    logging.info(f"处理文件夹: {subdir.name}")
                else:
                    # 如果文件夹中没有有效的CSV文件，记录但不标记为已处理
                    csv_count = len(list(subdir.glob('*.csv')))
                    if csv_count > 0:
                        logging.info(f"跳过文件夹 {subdir.name}: 包含 {csv_count} 个CSV文件，但无匹配的表名")
                    else:
                        logging.debug(f"跳过文件夹 {subdir.name}: 不包含CSV文件")
        
        # 记录找到的文件
        for table_name, files in csv_files.items():
            logging.info(f"找到表 {table_name} 的文件 {len(files)} 个")
        
        return dict(csv_files)
    

    
    def _merge_by_primary_key(self, data: pd.DataFrame, primary_key_cols: List[str]) -> pd.DataFrame:
        # 检查主键列是否在数据中
        missing = [c for c in primary_key_cols if c not in data.columns]
        if missing:
            logging.warning(f"主键列缺失: {missing}，无法执行合并操作")
            return pd.DataFrame(columns=data.columns)
        print(f"正在按主键 {primary_key_cols} 合并数据，初始行数: {len(data)}")
        if data.empty:
            return data

        if not primary_key_cols:
            return data.drop_duplicates(keep='first')

        # 使用字典来存储按主键分组的结果
        # 键是主键元组，值是该组的所有记录
        groups = {}

        # 首先按主键分组，处理空值的情况
        for idx, row in data.iterrows():
            # 构建主键元组，处理空值
            key_parts = []
            for pk in primary_key_cols:
                value = row[pk]
                if pd.isna(value) or value is None:
                    # 使用特殊标记表示空值
                    key_parts.append(None)
                else:
                    key_parts.append(str(value).strip())

            record_key = tuple(key_parts)

            if record_key not in groups:
                groups[record_key] = []
            groups[record_key].append(row.to_dict())

        # 合并每个组内的记录
        merged_records = []
        for key_parts, records in groups.items():
            if len(records) == 1:
                merged_records.append(records[0])
            else:
                # 合并该组内的所有记录
                merged_record = self._merge_duplicate_records(records)
                merged_records.append(merged_record)

        if merged_records:
            result_df = pd.DataFrame(merged_records)
            result_df = result_df.reindex(columns=data.columns)
            return result_df

        return pd.DataFrame(columns=data.columns)

    def _merge_duplicate_records(self, records: List[dict]) -> dict:
        """合并多条具有相同主键的记录"""
        if not records:
            return {}

        base_record = {}

        # 获取所有列
        all_columns = set()
        for record in records:
            all_columns.update(record.keys())

        for col in all_columns:
            values = []

            # 收集所有非空值
            for record in records:
                val = record.get(col)
                if pd.isna(val) or val is None:
                    continue

                str_val = str(val).strip()
                if str_val and str_val.lower() not in ['nan', 'none', 'null', '']:
                    values.append(str_val)

            if not values:
                base_record[col] = None
                continue

            # 去重
            unique_values = []
            seen = set()
            for val in values:
                if val not in seen:
                    unique_values.append(val)
                    seen.add(val)

            # 合并策略
            if len(unique_values) == 1:
                base_record[col] = unique_values[0]
            elif len(unique_values) > 1:
                # 检查是否有值已经是拼接字符串
                concatenated = []
                for val in unique_values:
                    if '+' in val:
                        parts = val.split('+')
                        concatenated.extend(parts)
                    else:
                        concatenated.append(val)

                # 再次去重
                final_unique = []
                final_seen = set()
                for val in concatenated:
                    if val not in final_seen:
                        final_unique.append(val)
                        final_seen.add(val)

                if len(final_unique) == 1:
                    base_record[col] = final_unique[0]
                else:
                    base_record[col] = '+'.join(final_unique)

        return base_record


    def run(self):
        """
        执行完整的合并流程
        """
        try:
            logging.info("开始数据合并流程...")
            
            # 初始化表结构
            self.initialize_table_structures()
            
            # 使用新的逐个文件夹处理方式
            self.process_folder_by_folder()
            
            logging.info("数据合并流程完成")
            
        except Exception as e:
            logging.error(f"数据合并过程中出错: {str(e)}")
            raise

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='数据合并工具')
    parser.add_argument('source_dir', nargs='?', default='H:\\##study\\数据集论文\\qqy_0412_output_v1_1_20260417_124119', help='源数据目录路径（默认为当前目录）')
    parser.add_argument('-o', '--output', default=None, help='输出目录路径（默认为源目录）')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, help='详细输出（默认关闭）')
    parser.add_argument('-m', '--memory-optimization', action='store_true', default=True, help='启用内存优化模式（每处理一个文件夹后清空内存）')
    
    args = parser.parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        # 创建合并器并运行
        merger = DataMerger(args.source_dir, args.output, args.memory_optimization)
        merger.run()
        
        print("数据合并完成！")
        
    except Exception as e:
        logging.error(f"程序执行失败: {str(e)}")
        sys.exit(1)

if __name__ == '__main__':
    main()