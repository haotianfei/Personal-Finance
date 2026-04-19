'use client'

import { useState, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { Steps, Button, Upload, Table, Radio, Alert, Card, Space, Typography, message } from 'antd'
import { UploadOutlined, FileOutlined, CheckCircleOutlined, ArrowLeftOutlined, ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons'
import type { UploadFile } from 'antd/es/upload/interface'
import Link from 'next/link'

const { Step } = Steps
const { Title, Text } = Typography

interface RulePreview {
  index: number
  name: string
  dimension: string
  target_id: string | null
  period_type: string
  compare_type: string
  amount_threshold: number | null
  percent_threshold: number | null
  direction: string
  is_active: boolean
  has_conflict: boolean
}

interface ImportResult {
  success: boolean
  imported: number
  skipped: number
  overwritten: number
  errors: string[]
  message: string
}

const DIMENSION_LABELS: Record<string, string> = {
  asset_name: '资产名称',
  fund_type: '资产类型',
  liquidity_rating: '流动性评级',
  account: '账户',
}

const PERIOD_TYPE_LABELS: Record<string, string> = {
  day: '日',
  month: '月',
  quarter: '季度',
  year: '年',
}

const COMPARE_TYPE_LABELS: Record<string, string> = {
  previous: '上一期',
  custom: '自定义',
}

const DIRECTION_LABELS: Record<string, string> = {
  both: '双向',
  up: '仅增长',
  down: '仅下降',
}

export default function AlertImportExportPage() {
  const router = useRouter()
  const [currentStep, setCurrentStep] = useState(0)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [previewData, setPreviewData] = useState<RulePreview[]>([])
  const [conflictCount, setConflictCount] = useState(0)
  const [conflictStrategy, setConflictStrategy] = useState<'skip' | 'overwrite'>('skip')
  const [importResult, setImportResult] = useState<ImportResult | null>(null)
  const [loading, setLoading] = useState(false)
  const fileContentRef = useRef<string>('')

  const steps = [
    {
      title: '上传文件',
      description: '选择JSON文件',
    },
    {
      title: '预览检查',
      description: '查看并处理冲突',
    },
    {
      title: '完成导入',
      description: '查看导入结果',
    },
  ]

  const handleUpload = async (file: File) => {
    if (!file.name.endsWith('.json')) {
      message.error('请上传 JSON 文件')
      return false
    }

    try {
      const content = await file.text()
      fileContentRef.current = content
      
      const response = await fetch('http://localhost:8000/api/alerts/import/preview', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: content,
      })

      if (!response.ok) {
        const error = await response.json()
        message.error(error.detail || '文件解析失败')
        return false
      }

      const data = await response.json()
      if (data.success) {
        setPreviewData(data.preview)
        setConflictCount(data.conflict_count)
        message.success(`文件解析成功，共 ${data.total} 条规则`)
        return true
      }
    } catch (error) {
      message.error('文件上传失败')
      console.error(error)
    }
    return false
  }

  const handleNext = async () => {
    if (currentStep === 0) {
      if (fileList.length === 0) {
        message.error('请先上传文件')
        return
      }
      const file = fileList[0].originFileObj
      if (file) {
        const success = await handleUpload(file)
        if (success) {
          setCurrentStep(1)
        }
      }
    } else if (currentStep === 1) {
      await executeImport()
    }
  }

  const handlePrev = () => {
    setCurrentStep(currentStep - 1)
  }

  const executeImport = async () => {
    if (fileList.length === 0 || !fileContentRef.current) {
      message.error('文件数据丢失，请重新上传')
      return
    }

    setLoading(true)
    try {
      const formData = new FormData()
      const blob = new Blob([fileContentRef.current], { type: 'application/json' })
      formData.append('file', blob, fileList[0].name)
      formData.append('conflict_strategy', conflictStrategy)

      const response = await fetch('http://localhost:8000/api/alerts/import/execute', {
        method: 'POST',
        body: formData,
      })

      if (!response.ok) {
        const error = await response.json()
        message.error(error.detail || '导入失败')
        setLoading(false)
        return
      }

      const data = await response.json()
      if (data.success) {
        setImportResult(data)
        setCurrentStep(2)
        message.success('导入完成')
      }
    } catch (error) {
      message.error('导入失败')
      console.error(error)
    } finally {
      setLoading(false)
    }
  }

  const handleReset = () => {
    setCurrentStep(0)
    setFileList([])
    setPreviewData([])
    setConflictCount(0)
    setConflictStrategy('skip')
    setImportResult(null)
    fileContentRef.current = ''
  }

  const columns = [
    {
      title: '规则名称',
      dataIndex: 'name',
      key: 'name',
      render: (text: string, record: RulePreview) => (
        <span className={record.has_conflict ? 'text-amber-600 font-medium' : ''}>
          {text}
          {record.has_conflict && (
            <span className="ml-2 text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded">
              冲突
            </span>
          )}
        </span>
      ),
    },
    {
      title: '维度',
      dataIndex: 'dimension',
      key: 'dimension',
      width: 100,
      render: (value: string) => DIMENSION_LABELS[value] || value,
    },
    {
      title: '账期类型',
      dataIndex: 'period_type',
      key: 'period_type',
      width: 80,
      render: (value: string) => PERIOD_TYPE_LABELS[value] || value,
    },
    {
      title: '对比方式',
      dataIndex: 'compare_type',
      key: 'compare_type',
      width: 100,
      render: (value: string) => COMPARE_TYPE_LABELS[value] || value,
    },
    {
      title: '金额阈值',
      dataIndex: 'amount_threshold',
      key: 'amount_threshold',
      width: 100,
      render: (value: number | null) => value !== null ? value.toFixed(2) : '-',
    },
    {
      title: '百分比阈值',
      dataIndex: 'percent_threshold',
      key: 'percent_threshold',
      width: 100,
      render: (value: number | null) => value !== null ? `${value.toFixed(2)}%` : '-',
    },
    {
      title: '方向',
      dataIndex: 'direction',
      key: 'direction',
      width: 80,
      render: (value: string) => DIRECTION_LABELS[value] || value,
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 60,
      render: (value: boolean) => value ? '启用' : '禁用',
    },
  ]

  const renderStepContent = () => {
    switch (currentStep) {
      case 0:
        return (
          <Card className="mt-6">
            <Alert
              message="导入说明"
              description="请上传 JSON 格式的预警规则文件。文件应包含规则名称、维度、账期类型、阈值等完整信息。"
              type="info"
              showIcon
              className="mb-6"
            />
            <Upload.Dragger
              beforeUpload={(file) => {
                if (!file.name.endsWith('.json')) {
                  message.error('请上传 JSON 文件')
                  return false
                }
                return false
              }}
              onChange={(info) => {
                setFileList(info.fileList)
              }}
              fileList={fileList}
              maxCount={1}
              accept=".json"
            >
              <p className="ant-upload-drag-icon">
                <FileOutlined style={{ fontSize: 48, color: '#1890ff' }} />
              </p>
              <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
              <p className="ant-upload-hint">仅支持 JSON 格式文件</p>
            </Upload.Dragger>
          </Card>
        )

      case 1:
        return (
          <Card className="mt-6">
            {conflictCount > 0 && (
              <Alert
                message={`检测到 ${conflictCount} 条同名规则冲突`}
                description="请选择冲突处理策略："
                type="warning"
                showIcon
                className="mb-4"
              />
            )}
            {conflictCount === 0 && (
              <Alert
                message="未检测到冲突"
                description="所有规则均为新规则，可以直接导入。"
                type="success"
                showIcon
                className="mb-4"
              />
            )}
            
            {conflictCount > 0 && (
              <Radio.Group
                value={conflictStrategy}
                onChange={(e) => setConflictStrategy(e.target.value)}
                className="mb-4"
              >
                <Radio value="skip">跳过冲突规则（保留现有规则）</Radio>
                <Radio value="overwrite">覆盖冲突规则（使用导入的规则）</Radio>
              </Radio.Group>
            )}

            <Table
              dataSource={previewData}
              columns={columns}
              rowKey="index"
              pagination={false}
              size="small"
              scroll={{ x: 'max-content' }}
            />
          </Card>
        )

      case 2:
        return (
          <Card className="mt-6">
            {importResult && (
              <>
                <Alert
                  message="导入完成"
                  description={importResult.message}
                  type="success"
                  showIcon
                  className="mb-6"
                />
                
                <div className="grid grid-cols-3 gap-4 mb-6">
                  <Card className="text-center">
                    <div className="text-3xl font-bold text-green-600">{importResult.imported}</div>
                    <div className="text-sm text-slate-500 mt-1">新增规则</div>
                  </Card>
                  <Card className="text-center">
                    <div className="text-3xl font-bold text-blue-600">{importResult.overwritten}</div>
                    <div className="text-sm text-slate-500 mt-1">覆盖规则</div>
                  </Card>
                  <Card className="text-center">
                    <div className="text-3xl font-bold text-amber-600">{importResult.skipped}</div>
                    <div className="text-sm text-slate-500 mt-1">跳过规则</div>
                  </Card>
                </div>

                {importResult.errors.length > 0 && (
                  <Alert
                    message="导入过程中的错误"
                    description={
                      <ul className="list-disc pl-4 mt-2">
                        {importResult.errors.map((error, idx) => (
                          <li key={idx} className="text-red-600">{error}</li>
                        ))}
                      </ul>
                    }
                    type="error"
                    className="mb-6"
                  />
                )}

                <div className="flex justify-center gap-4">
                  <Button icon={<ReloadOutlined />} onClick={handleReset}>
                    继续导入
                  </Button>
                  <Link href="/alerts">
                    <Button type="primary" icon={<CheckCircleOutlined />}>
                      返回预警页面
                    </Button>
                  </Link>
                </div>
              </>
            )}
          </Card>
        )

      default:
        return null
    }
  }

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">导入预警规则</h1>
          <p className="text-sm text-slate-500 mt-1">通过引导步骤批量导入预警规则</p>
        </div>
        <Link
          href="/alerts"
          className="flex items-center gap-2 px-4 py-2 text-slate-600 hover:text-slate-800 transition-colors"
        >
          <ArrowLeftOutlined />
          返回
        </Link>
      </div>

      <Steps current={currentStep} className="mb-8">
        {steps.map((step, index) => (
          <Step key={index} title={step.title} description={step.description} />
        ))}
      </Steps>

      {renderStepContent()}

      {currentStep < 2 && (
        <div className="flex justify-between mt-6">
          <Button
            onClick={handlePrev}
            disabled={currentStep === 0}
            icon={<ArrowLeftOutlined />}
          >
            上一步
          </Button>
          <Button
            type="primary"
            onClick={handleNext}
            loading={loading}
            icon={currentStep === 1 ? <CheckCircleOutlined /> : <ArrowRightOutlined />}
          >
            {currentStep === 1 ? '确认导入' : '下一步'}
          </Button>
        </div>
      )}
    </div>
  )
}
