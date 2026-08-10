import { useEffect, useState } from 'react';
import { CheckOutlined, CloseOutlined, EditOutlined } from '@ant-design/icons';
import { Button, Input, Space } from 'antd';
import type { ImageAssetManagementItem } from '../types/product';
import {
  ImageAssetRenameError,
  renameImageAsset,
} from '../services/productApi';

type RenameFunction = typeof renameImageAsset;

export interface AssetDisplayNameEditorProps {
  asset: ImageAssetManagementItem;
  renameAsset?: RenameFunction;
  onRenamed?: (asset: ImageAssetManagementItem) => void;
}

const sourceExtension = (sourceRelativePath: string): string => {
  const basename = sourceRelativePath.split('/').pop() || sourceRelativePath;
  const dotIndex = basename.lastIndexOf('.');
  return dotIndex > 0 && dotIndex < basename.length - 1
    ? basename.slice(dotIndex)
    : '';
};

const displayNameBody = (
  displayName: string,
  sourceRelativePath: string
): string => {
  const extension = sourceExtension(sourceRelativePath);
  return extension && displayName.endsWith(extension)
    ? displayName.slice(0, -extension.length)
    : displayName;
};

export function AssetDisplayNameEditor({
  asset,
  renameAsset = renameImageAsset,
  onRenamed,
}: AssetDisplayNameEditorProps) {
  const [serverAsset, setServerAsset] = useState(asset);
  const [editing, setEditing] = useState(false);
  const [draftBody, setDraftBody] = useState(
    displayNameBody(asset.display_name, asset.source_relative_path)
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setServerAsset(asset);
    if (!editing) {
      setDraftBody(displayNameBody(asset.display_name, asset.source_relative_path));
    }
  // Props only synchronize when the parent representation changes. Editing
  // transitions must not overwrite a just-saved local server representation.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asset]);

  const beginEditing = () => {
    setDraftBody(displayNameBody(
      serverAsset.display_name,
      serverAsset.source_relative_path
    ));
    setError(null);
    setEditing(true);
  };

  const cancelEditing = () => {
    setDraftBody(displayNameBody(
      serverAsset.display_name,
      serverAsset.source_relative_path
    ));
    setError(null);
    setEditing(false);
  };

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const renamed = await renameAsset(
        serverAsset.asset_id,
        draftBody,
        serverAsset.version
      );
      setServerAsset(renamed);
      setDraftBody(displayNameBody(
        renamed.display_name,
        renamed.source_relative_path
      ));
      setEditing(false);
      onRenamed?.(renamed);
    } catch (caught) {
      if (caught instanceof ImageAssetRenameError && caught.latest) {
        setServerAsset(caught.latest);
        setError(
          `${caught.message}。服务器最新名称：${caught.latest.display_name}`
        );
      } else {
        setError(caught instanceof Error ? caught.message : '图片资产改名失败');
      }
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <div className="asset-display-name-row">
        <span className="asset-display-name" title={serverAsset.display_name}>
          {serverAsset.display_name}
        </span>
        <Button
          type="text"
          size="small"
          icon={<EditOutlined />}
          aria-label={`编辑显示名称 ${serverAsset.display_name}`}
          onClick={beginEditing}
        />
      </div>
    );
  }

  return (
    <div className="asset-display-name-editor">
      <Input
        aria-label="显示名称主体"
        value={draftBody}
        addonAfter={sourceExtension(serverAsset.source_relative_path)}
        disabled={saving}
        onChange={(event) => setDraftBody(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            void save();
          } else if (event.key === 'Escape') {
            event.preventDefault();
            cancelEditing();
          }
        }}
      />
      <Space size={4}>
        <Button
          size="small"
          type="primary"
          icon={<CheckOutlined />}
          aria-label="保存显示名称"
          loading={saving}
          onClick={() => void save()}
        />
        <Button
          size="small"
          icon={<CloseOutlined />}
          aria-label="取消修改显示名称"
          disabled={saving}
          onClick={cancelEditing}
        />
      </Space>
      {error && <div className="asset-display-name-error" role="alert">{error}</div>}
    </div>
  );
}
