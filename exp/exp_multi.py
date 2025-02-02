import os
import torch
import numpy as np
from utils.metrics import metric
from utils import logger
from utils.tools import EarlyStopping, adjust_learning_rate
from tqdm import tqdm
import time
from exp.exp_basic import Exp_Basic
from utils.visualization import plot_pred, map_plot_function, \
plot_values_distribution, plot_error_distribution, plot_errors_threshold, plot_visual_sample
class Exp_Multi(Exp_Basic):
    def __init__(self, args):
        super().__init__(args)
    def train(self, setting):

        path = os.path.join(self.args.checkpoints, setting)
        if not os.path.exists(path):
            os.makedirs(path)
        best_model_path = path+'/'+'checkpoint.pth'

        # 读取上次训练模型
        if self.args.load:
            if "checkpoint.pth" in path:
                print("---------------------load last trained model--------------------------")
                self.model.load_state_dict(torch.load(best_model_path))

        early_stopping = EarlyStopping(patience=self.args.patience, verbose=True)
        
        model_optim = self._select_optimizer()
        criterion =  self._select_criterion()

        if self.args.use_amp:
            scaler = torch.cuda.amp.GradScaler()

        for idx_epoch in range(self.args.train_epochs):
            self.model.train()

            epoch_time = time.time()
            epoch_train_steps_count, epoch_train_steps = 0, 0
            total_train_loss = []
            for file_idx, file_name in enumerate(tqdm(self.fileName_lst), 1):
                train_data, train_loader = self._get_data(file_name = file_name, flag = 'train')
                train_steps = len(train_loader)
                epoch_train_steps_count += train_steps

                running_loss = 0
                for idx_batch, batch in enumerate(train_loader):
                    epoch_train_steps += 1
                    
                    model_optim.zero_grad()
                    batch_out= self.process_one_batch(train_data, batch)
                    loss = criterion(*batch_out)
                    running_loss += loss.item()
                    
                    if (idx_batch+1) % 1000==0:
                        logger.info("Epoch: {0}, file_idx: {1}, epoch_train_steps: {2},  | loss: {3:.7f}".format(idx_epoch + 1, file_idx, epoch_train_steps, loss.item()))
                    
                    if self.args.use_amp:
                        scaler.scale(loss).backward()
                        scaler.step(model_optim)
                        scaler.update()
                    else:
                        loss.backward()
                        model_optim.step()
                # file_idx
                train_loss = running_loss/len(train_loader)
                total_train_loss.append(train_loss)
                logger.info("Epoch: {} file_idx: {} train_loss: {}".format(idx_epoch+1, file_idx, train_loss))

            total_vali_loss, vali_metrics = self.vali("val", criterion)
            total_train_loss = np.average(total_train_loss)
            # epoch损失记录
            logger.info("Epoch: {}, epoch_train_steps: {} | Train Loss: {:.7f} Vali Loss: {:.7f} cost time: {}".format(
                idx_epoch + 1, epoch_train_steps, total_train_loss, total_vali_loss, (time.time()-epoch_time)/60))
            
            early_stopping(total_vali_loss, self.model, path)
            if early_stopping.early_stop:
                print("Early stopping")
                break

            adjust_learning_rate(model_optim, idx_epoch+1, self.args)
            
        self.model.load_state_dict(torch.load(best_model_path))
        return self.model

    def _vali(self, val_data, val_loader, criterion):
        # 区别于_test, 不需要保存和loss测度
        self.model.eval()
        
        preds, trues = [], []
        running_loss = 0
        for i, batch in enumerate(val_loader):
            pred, true = self.process_one_batch(val_data, batch)
            pred, true = pred.detach().cpu(), true.detach().cpu()
            preds.append(pred); trues.append(true)
            
            _loss = criterion(pred, true)
            running_loss += _loss.item()
            
        preds, trues = np.concatenate(preds), np.concatenate(trues)
        loss = running_loss/len(val_loader)
        return preds, trues, loss

    def vali(self, flag, criterion):
        
        total_loss, total_preds, total_trues =[], []
        for file_name in self.fileName_lst:
            val_data, val_loader = self._get_data(file_name, flag=flag)
            preds, trues, loss = self._vali(val_data, val_loader, criterion)
            total_preds.append(preds)
            total_trues.append(trues)
            total_loss.append(loss)

        total_trues, total_preds = np.concatenate(total_trues), np.concatenate(total_preds)
        mae, mse, rmse, mape, mspe = metric(total_preds, total_trues)
        total_loss = np.average(total_loss)

        self.model.train()
        return total_loss, (mae, mse, rmse, mape, mspe)

    def _test(self, test_data, test_loader, file_path):                
        self.model.eval()
        preds_lst, trues_lst = [], []
        for i, batch in enumerate(test_loader):
            pred, true = self.process_one_batch(test_data, batch)
            preds_lst.append(pred.detach().cpu()); trues_lst.append(true.detach().cpu())
        
        preds, trues = np.concatenate(preds_lst), np.concatenate(trues_lst)
        logger.debug('test shape:{} {}'.format(preds.shape, trues.shape))
        
        mae, mse, rmse, mape, mspe = metric(preds, trues)
        print('mse:{}, mae:{}'.format(mse, mae))

        if file_path is not None:
            np.save(f'{file_path}_metrics.npy', np.array([mae, mse, rmse, mape, mspe]))
            np.save(f'{file_path}_pred.npy', preds)
            np.save(f'{file_path}_true.npy', trues)

        return preds, trues

    def test(self, setting, load=False, plot=True):
        # test承接train之后模型，为保证单独使用test，增加load参数
        if load:
            path = os.path.join(self.args.checkpoints, setting)
            best_model_path = path+'/'+'checkpoint.pth'
            self.model.load_state_dict(torch.load(best_model_path))

        # result save
        folder_path = './results/' + setting +'/'
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
        
        total_preds_lst, total_trues_lst = [], []
        
        for file_name in self.fileName_lst:
            test_data, test_loader = self._get_data(file_name, flag='test')

            file_path = folder_path+f'{file_name[:-4]}' if len(self.fileName_lst)>1 else None
            preds, trues = self._test(test_data, test_loader, file_path)
            # inverse
            preds = test_data.inverse_transform(preds)[..., -1:]
            trues = test_data.inverse_transform(trues)[..., -1:]
            total_preds_lst.append(preds)
            total_trues_lst.append(trues)
        
        total_trues, total_preds = np.concatenate(total_trues_lst), np.concatenate(total_preds_lst)
        # total_preds = np.where(abs(total_preds)>10, 0, total_preds)
        # total_trues = np.where(abs(total_trues)>1, 0, total_trues)
        logger.info("test shape:{} {}".format(total_preds.shape, total_trues.shape))
        mae, mse, rmse, mape, mspe = metric(total_preds, total_trues)
        print('mse:{}, mae:{}'.format(mse, mae))

        np.save(folder_path+'metrics.npy', np.array([mae, mse, rmse, mape, mspe]))
        np.save(folder_path+'pred.npy', total_preds)
        np.save(folder_path+f'true.npy', total_trues)
        if plot:
            # plot_pred(total_trues, total_preds)
            if self.args.pred_len > 1:
                map_plot_function(total_trues, total_preds, 
                plot_values_distribution, ['volitility'], [0], self.args.pred_len)
            else:
                map_plot_function(total_trues.reshape(120, -1, 1), total_preds.reshape(120, -1, 1), 
                plot_values_distribution, ['volitility'], [0], 6)